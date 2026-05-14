#!/usr/bin/env python3
"""
ollama_switch.py — пульт управления Ollama и мониторингом Cilly для AI-офиса.

Команды:
  status                       — показать текущее состояние всех ботов
  
  on / off                     — включить/выключить Ollama на ВСЕХ ботах
  on <bot> / off <bot>         — только на одном боте (billy/tilly/.../mama)

  pause                        — приостановить мониторинг логов в Cilly
                                 (используй ПЕРЕД массовыми деплоями, иначе Cilly
                                 примет conflict-ошибки рестарта за реальные баги
                                 и будет жечь Claude на анализ)
  resume                       — снять паузу с мониторинга Cilly
"""

import sys, json, urllib.request

RAILWAY_TOKEN = "f21b20a0-3b7e-4746-a991-981f5679afc6"
PROJECT = "271b40b7-199a-429a-88ef-ca417f26a638"
ENV = "2efaaf60-ba39-492c-bf86-007fd505493f"

BOTS = {
    "billy":   "b441ce93-9736-49b3-9b5d-d0c82e715b28",
    "tilly":   "367e25d7-8410-419d-896d-2cc86cd44efd",
    "milly":   "db277aff-6638-4b4a-970e-b016bd753608",
    "doctor":  "d949c4d2-59fa-4cbe-8bb8-a0589a476607",
    "prophet": "9db4108e-19f1-4c1f-a21c-3909442e137c",
    "gosling": "ed03c9d3-e83f-4675-9f0a-a4d4fc622365",
    "villy":   "a5e37cc4-0a9f-4700-b6d3-d39b958ce0cb",
    "kriss":   "92f70bbb-70ea-474c-be0d-5cc1c9bd8f4e",
    "mama":    "fa7c87cf-454c-4946-ab25-6a5091f0ac47",
}
CILLY = "efa6bd21-91d8-467f-8250-60f8a3853791"


def _call(query, variables=None):
    body = {"query": query}
    if variables:
        body["variables"] = variables
    req = urllib.request.Request(
        "https://backboard.railway.com/graphql/v2",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {RAILWAY_TOKEN}", "Content-Type": "application/json",
                 "User-Agent": "curl/8.0.0"},
    )
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def _upsert(sid, name, value):
    return _call(
        "mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }",
        {"input": {"projectId": PROJECT, "environmentId": ENV, "serviceId": sid,
                   "name": name, "value": value}}
    )["data"]["variableUpsert"]


def _get_var(sid, name):
    q = f'query {{ variables(projectId: "{PROJECT}", environmentId: "{ENV}", serviceId: "{sid}") }}'
    return _call(q)["data"]["variables"].get(name, "(не задано)")


def cmd_status():
    print(f"\n{'bot':<10} OLLAMA_ENABLED")
    print("-" * 30)
    for name, sid in BOTS.items():
        print(f"{name:<10} {_get_var(sid, 'OLLAMA_ENABLED')}")
    print(f"\n{'cilly':<10} CILLY_MONITOR_PAUSED = {_get_var(CILLY, 'CILLY_MONITOR_PAUSED')}")


def cmd_ollama(action, target):
    targets = {target: BOTS[target]} if target else BOTS
    if target and target not in BOTS:
        print(f"Unknown bot: {target}. Available: {', '.join(BOTS)}")
        sys.exit(1)
    new_val = "true" if action == "on" else "false"
    print(f"\nOLLAMA_ENABLED={new_val} → {len(targets)} bot(s):")
    for name, sid in targets.items():
        try:
            ok = _upsert(sid, "OLLAMA_ENABLED", new_val)
            print(f"  {name:<10} {'✓' if ok else '✗'}")
        except Exception as e:
            print(f"  {name:<10} ✗ {e}")
    print(f"\nRailway пересоберёт затронутые сервисы за 1-2 минуты.")


def cmd_pause(paused: bool):
    val = "true" if paused else "false"
    action = "PAUSE" if paused else "RESUME"
    print(f"\n{action} Cilly monitor: CILLY_MONITOR_PAUSED={val}")
    ok = _upsert(CILLY, "CILLY_MONITOR_PAUSED", val)
    print(f"  cilly      {'✓' if ok else '✗'}")
    if paused:
        print("\nCilly перестанет анализировать логи через ~1 мин (после пересборки).")
        print("ОБЯЗАТЕЛЬНО снять паузу через `resume` когда массовый деплой закончится!")
    else:
        print("\nCilly возобновит мониторинг через ~1 мин.")


def main():
    if len(sys.argv) < 2:
        print(__doc__); sys.exit(1)
    
    cmd = sys.argv[1]
    if cmd == "status":
        cmd_status()
    elif cmd in ("on", "off"):
        target = sys.argv[2].lower().replace("-bot", "") if len(sys.argv) > 2 else None
        cmd_ollama(cmd, target)
    elif cmd == "pause":
        cmd_pause(True)
    elif cmd == "resume":
        cmd_pause(False)
    else:
        print(__doc__); sys.exit(1)


if __name__ == "__main__":
    main()
