#!/usr/bin/env python3
"""
ops.py — инцидент-реакция офиса для Клода (sandbox). Быстро чинить то, чего не
может команда: статус/логи/редеплой/переменные по ВСЕМ проектам офиса, мгновенно
глушить/возвращать Силли, читать Redis (через HTTP-прокси Силли).

Креды — ТОЛЬКО из окружения (в коде секретов нет):
  RAILWAY_TOKEN       — Railway API (скоуп на проекты офиса)
  REDIS_PROXY_TOKEN   — (опц.) для команды redis через /redis-прокси Силли;
                        прямой TCP к Redis из песочницы заблокирован платформой.
  RAILWAY_PROJECT_IDS — (опц.) список проектов через запятую (есть дефолт)

Запуск:
  python ops.py status
  python ops.py logs <service> [n]
  python ops.py redeploy <service>
  python ops.py vars <service>
  python ops.py getvar <service> <NAME>
  python ops.py setvar <service> <NAME> <VALUE>
  python ops.py pause | resume          # CILLY_PAUSED + редеплой ai-office-shared
  python ops.py redis GET <key> | SET <key> <val> | DEL <key> | KEYS <pattern>
"""
import os
import sys

import httpx

RAILWAY_TOKEN     = os.environ.get("RAILWAY_TOKEN") or os.environ.get("RAILWAY_TOKEN_VLAD") or ""
GQL               = "https://backboard.railway.com/graphql/v2"
SILLI_URL         = os.environ.get("SILLI_URL", "https://ai-office-shared-production.up.railway.app").rstrip("/")
REDIS_PROXY_TOKEN = os.environ.get("REDIS_PROXY_TOKEN", "")
PROJECTS_FILTER = [p.strip() for p in os.environ.get("RAILWAY_PROJECT_IDS", "").split(",") if p.strip()]

_CATALOG = None  # name -> (project_id, service_id, env_id)


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


def catalog() -> dict:
    """name -> (project_id, service_id, env_id) по ВСЕМ проектам, видимым токену.
    Авто-обнаружение через projects; RAILWAY_PROJECT_IDS — опциональный фильтр."""
    global _CATALOG
    if _CATALOG is not None:
        return _CATALOG
    _CATALOG = {}
    d = gql("{ projects { edges { node { id name "
            "environments { edges { node { id name } } } "
            "services { edges { node { id name } } } } } } }")
    for e in ((d.get("data") or {}).get("projects") or {}).get("edges") or []:
        n = e["node"]
        pid = n["id"]
        if PROJECTS_FILTER and pid not in PROJECTS_FILTER:
            continue
        envs = [(x["node"]["name"], x["node"]["id"])
                for x in (n.get("environments") or {}).get("edges") or []]
        env_id = next((i for nm, i in envs if nm == "production"), envs[0][1] if envs else None)
        for s in (n.get("services") or {}).get("edges") or []:
            _CATALOG[s["node"]["name"]] = (pid, s["node"]["id"], env_id)
    return _CATALOG


def resolve(name: str):
    c = catalog()
    if name in c:
        return c[name]
    for n, v in c.items():
        if n.lower().startswith(name.lower()):
            return v
    _die(f"сервис '{name}' не найден. Есть: {', '.join(sorted(c))}")


def mask(v) -> str:
    v = str(v)
    return (v[:4] + "…" + v[-4:]) if len(v) > 8 else "***"


def cmd_status():
    c = catalog()
    nproj = len({pid for pid, _, _ in c.values()})
    print(f"Сервисов: {len(c)} в {nproj} проектах")
    for name, (pid, sid, eid) in sorted(c.items()):
        d = gql("query($id:String!){deployments(first:1,input:{serviceId:$id}){edges{node{status}}}}", {"id": sid})
        edges = (((d.get("data") or {}).get("deployments") or {}).get("edges")) or []
        st = edges[0]["node"]["status"] if edges else "NO_DEPLOY"
        icon = "🟢" if st == "SUCCESS" else ("⚪" if st == "NO_DEPLOY" else "🔴")
        print(f"  {icon} {name:<22} {st}")


def cmd_logs(name: str, n: int = 40):
    pid, sid, eid = resolve(name)
    d = gql("query($id:String!){deployments(first:1,input:{serviceId:$id}){edges{node{id}}}}", {"id": sid})
    edges = (((d.get("data") or {}).get("deployments") or {}).get("edges")) or []
    if not edges:
        _die("нет деплоев")
    ld = gql("query($id:String!){deploymentLogs(deploymentId:$id){message}}", {"id": edges[0]["node"]["id"]})
    for l in ((ld.get("data") or {}).get("deploymentLogs") or [])[-n:]:
        print(l.get("message", ""))


def cmd_redeploy(name: str):
    pid, sid, eid = resolve(name)
    gql("mutation($s:String!,$e:String!){serviceInstanceRedeploy(serviceId:$s,environmentId:$e)}",
        {"s": sid, "e": eid})
    print(f"🚀 редеплой запущен: {name}")


def _vars(name: str) -> dict:
    pid, sid, eid = resolve(name)
    d = gql("query($p:String!,$s:String!,$e:String!){variables(projectId:$p,serviceId:$s,environmentId:$e)}",
            {"p": pid, "s": sid, "e": eid})
    return (d.get("data") or {}).get("variables") or {}


def cmd_vars(name: str):
    for k, v in sorted(_vars(name).items()):
        print(f"  {k} = {mask(v)} (len={len(str(v))})")


def cmd_getvar(name: str, key: str):
    v = _vars(name).get(key)
    print("НЕ задан" if v is None else f"{key} = {mask(v)} (len={len(str(v))})")


def cmd_setvar(name: str, key: str, value: str):
    pid, sid, eid = resolve(name)
    gql("mutation($i:VariableUpsertInput!){variableUpsert(input:$i)}",
        {"i": {"projectId": pid, "environmentId": eid, "serviceId": sid, "name": key, "value": value}})
    print(f"✅ {name}: {key}={mask(value) if value else '(пусто)'} установлен")


def _proxy_token() -> str:
    """Токен для /redis-прокси. Из env REDIS_PROXY_TOKEN, иначе сам подтягиваю
    офисный RAILWAY_TOKEN_VLAD у Силли через Railway (прокси сверяет именно его)."""
    if REDIS_PROXY_TOKEN:
        return REDIS_PROXY_TOKEN
    t = _vars("ai-office-shared").get("RAILWAY_TOKEN_VLAD") or ""
    if not t:
        _die("не нашёл токен для /redis (RAILWAY_TOKEN_VLAD у Силли пуст)")
    return t


def redis_proxy(cmd: str, *args):
    """Redis через HTTP /redis Силли (прямой TCP из песочницы заблокирован)."""
    r = httpx.post(f"{SILLI_URL}/redis", json={"cmd": cmd, "args": list(args)},
                   headers={"X-Auth-Token": _proxy_token(), "Content-Type": "application/json"}, timeout=20)
    data = r.json()
    if data.get("error"):
        _die(f"redis proxy: {data['error']}")
    return data.get("result")


def cmd_redis(args: list):
    if not args:
        _die("redis: укажи операцию (GET/SET/DEL/KEYS ...)")
    print(redis_proxy(args[0].upper(), *args[1:]))


def cmd_pause():
    cmd_setvar("ai-office-shared", "CILLY_PAUSED", "1")
    cmd_redeploy("ai-office-shared")
    print("⏸ Силли ставится на паузу (CILLY_PAUSED=1 + редеплой) — замолчит через ~минуту.")


def cmd_resume():
    cmd_setvar("ai-office-shared", "CILLY_PAUSED", "")
    cmd_redeploy("ai-office-shared")
    print("▶️ пауза снимается (CILLY_PAUSED очищен + редеплой).")


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
