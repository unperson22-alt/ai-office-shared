#!/usr/bin/env python3
"""
ops.py — инцидент-реакция офиса для Клода (sandbox). Быстро чинить то, чего не
может команда: смотреть статус/логи, редеплоить, читать/ставить переменные,
дёргать Redis, мгновенно глушить/возвращать Силли.

Креды — ТОЛЬКО из окружения (в коде секретов нет):
  RAILWAY_TOKEN  (или RAILWAY_TOKEN_VLAD) — Railway API
  REDIS_URL      — прямой доступ к Redis (работает даже если Силли лежит)

Запуск:
  python ops.py status
  python ops.py logs <service> [n]
  python ops.py redeploy <service>
  python ops.py vars <service>
  python ops.py getvar <service> <NAME>
  python ops.py setvar <service> <NAME> <VALUE>
  python ops.py pause | resume          # глушит/возвращает Силли (Redis cilly:paused)
  python ops.py redis GET <key> | SET <key> <val> | DEL <key> | KEYS <pattern>
"""
import os
import sys
import json

import httpx

RAILWAY_TOKEN  = os.environ.get("RAILWAY_TOKEN") or os.environ.get("RAILWAY_TOKEN_VLAD") or ""
REDIS_URL      = os.environ.get("REDIS_URL", "")
GQL            = "https://backboard.railway.com/graphql/v2"
PROJECT_ID     = os.environ.get("RAILWAY_PROJECT_ID", "271b40b7-199a-429a-88ef-ca417f26a638")
ENVIRONMENT_ID = os.environ.get("RAILWAY_ENV_ID", "2efaaf60-ba39-492c-bf86-007fd505493f")


def _die(msg: str, code: int = 1):
    print(f"❌ {msg}", file=sys.stderr)
    sys.exit(code)


def gql(query: str, variables: dict | None = None) -> dict:
    if not RAILWAY_TOKEN:
        _die("RAILWAY_TOKEN (или RAILWAY_TOKEN_VLAD) не задан в окружении.")
    r = httpx.post(
        GQL,
        json={"query": query, "variables": variables or {}},
        headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json"},
        timeout=30,
    )
    data = r.json()
    if data.get("errors"):
        _die("Railway API: " + "; ".join(e.get("message", str(e)) for e in data["errors"]))
    return data


def services() -> dict:
    """name -> service_id по проекту."""
    data = gql(
        "query($id:String!){project(id:$id){services{edges{node{id name}}}}}",
        {"id": PROJECT_ID},
    )
    edges = (((data.get("data") or {}).get("project") or {}).get("services") or {}).get("edges") or []
    return {e["node"]["name"]: e["node"]["id"] for e in edges}


def resolve(name: str) -> str:
    svc = services()
    if name in svc:
        return svc[name]
    # мягкий матч (billy / billy-bot)
    for n, sid in svc.items():
        if n.lower().startswith(name.lower()):
            return sid
    _die(f"сервис '{name}' не найден. Есть: {', '.join(sorted(svc))}")


def mask(v: str) -> str:
    v = str(v)
    return (v[:4] + "…" + v[-4:]) if len(v) > 8 else "***"


def cmd_status():
    svc = services()
    print(f"Сервисов: {len(svc)} (проект {PROJECT_ID})")
    for name, sid in sorted(svc.items()):
        d = gql(
            "query($id:String!){deployments(first:1,input:{serviceId:$id}){edges{node{status}}}}",
            {"id": sid},
        )
        edges = (((d.get("data") or {}).get("deployments") or {}).get("edges")) or []
        status = edges[0]["node"]["status"] if edges else "NO_DEPLOY"
        icon = "🟢" if status == "SUCCESS" else "🔴"
        print(f"  {icon} {name:<22} {status}")


def cmd_logs(name: str, n: int = 40):
    sid = resolve(name)
    d = gql(
        "query($id:String!){deployments(first:1,input:{serviceId:$id}){edges{node{id status}}}}",
        {"id": sid},
    )
    edges = (((d.get("data") or {}).get("deployments") or {}).get("edges")) or []
    if not edges:
        _die("нет деплоев")
    dep_id = edges[0]["node"]["id"]
    ld = gql("query($id:String!){deploymentLogs(deploymentId:$id){message timestamp}}", {"id": dep_id})
    logs = (ld.get("data") or {}).get("deploymentLogs") or []
    for l in logs[-n:]:
        print(l.get("message", ""))


def cmd_redeploy(name: str):
    sid = resolve(name)
    gql(
        "mutation($s:String!,$e:String!){serviceInstanceRedeploy(serviceId:$s,environmentId:$e)}",
        {"s": sid, "e": ENVIRONMENT_ID},
    )
    print(f"🚀 редеплой запущен: {name}")


def _vars(sid: str) -> dict:
    d = gql(
        "query($p:String!,$s:String!,$e:String!){variables(projectId:$p,serviceId:$s,environmentId:$e)}",
        {"p": PROJECT_ID, "s": sid, "e": ENVIRONMENT_ID},
    )
    return (d.get("data") or {}).get("variables") or {}


def cmd_vars(name: str):
    for k, v in sorted(_vars(resolve(name)).items()):
        print(f"  {k} = {mask(v)} (len={len(str(v))})")


def cmd_getvar(name: str, key: str):
    v = _vars(resolve(name)).get(key)
    print("НЕ задан" if v is None else f"{key} = {mask(v)} (len={len(str(v))})")


def cmd_setvar(name: str, key: str, value: str):
    sid = resolve(name)
    gql(
        "mutation($i:VariableUpsertInput!){variableUpsert(input:$i)}",
        {"i": {"projectId": PROJECT_ID, "environmentId": ENVIRONMENT_ID,
               "serviceId": sid, "name": key, "value": value}},
    )
    print(f"✅ {name}: {key} = {mask(value)} установлен (редеплой при необходимости: ops.py redeploy {name})")


def _redis():
    if not REDIS_URL:
        _die("REDIS_URL не задан в окружении.")
    try:
        import redis  # lazy
    except ImportError:
        _die("нет пакета redis — pip install redis")
    return redis.from_url(REDIS_URL, decode_responses=True)


def cmd_redis(args: list):
    r = _redis()
    op = args[0].upper()
    if op == "GET":
        print(r.get(args[1]))
    elif op == "SET":
        r.set(args[1], args[2]); print("OK")
    elif op == "DEL":
        print(r.delete(args[1]))
    elif op == "KEYS":
        for k in r.keys(args[1] if len(args) > 1 else "*"):
            print(k)
    else:
        _die(f"redis: неизвестная операция {op}")


def cmd_pause():
    _redis().set("cilly:paused", "1")
    print("⏸ Силли на паузе (cilly:paused=1). Исходящие в группу подавлены. ops.py resume — снять.")


def cmd_resume():
    _redis().delete("cilly:paused")
    print("▶️ пауза снята (cilly:paused удалён).")


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(0)
    cmd, rest = sys.argv[1], sys.argv[2:]
    try:
        if cmd == "status":      cmd_status()
        elif cmd == "logs":      cmd_logs(rest[0], int(rest[1]) if len(rest) > 1 else 40)
        elif cmd == "redeploy":  cmd_redeploy(rest[0])
        elif cmd == "vars":      cmd_vars(rest[0])
        elif cmd == "getvar":    cmd_getvar(rest[0], rest[1])
        elif cmd == "setvar":    cmd_setvar(rest[0], rest[1], rest[2])
        elif cmd == "redis":     cmd_redis(rest)
        elif cmd == "pause":     cmd_pause()
        elif cmd == "resume":    cmd_resume()
        else:                    _die(f"неизвестная команда: {cmd}")
    except IndexError:
        _die("не хватает аргументов — см. ops.py без аргументов для справки")


if __name__ == "__main__":
    main()
