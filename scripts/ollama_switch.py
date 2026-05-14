#!/usr/bin/env python3
"""
ollama_switch.py — массовый переключатель Ollama для AI-офиса.

Использование:
  python ollama_switch.py off          # выключить Ollama на всех ботах (fallback на Claude)
  python ollama_switch.py on           # включить Ollama на всех ботах
  python ollama_switch.py status       # показать текущее состояние

  python ollama_switch.py off billy    # выключить только на конкретном боте
  python ollama_switch.py on tilly     # включить только на конкретном

После переключения Railway автоматически перезапустит затронутые сервисы.
"""

import sys, json, urllib.request

RAILWAY_TOKEN = "f21b20a0-3b7e-4746-a991-981f5679afc6"
PROJECT = "271b40b7-199a-429a-88ef-ca417f26a638"
ENV = "2efaaf60-ba39-492c-bf86-007fd505493f"

# Боты с поддержкой Ollama-fallback (cilly умышленно НЕ включён)
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


def set_enabled(sid, value: str):
    return _call(
        "mutation Upsert($input: VariableUpsertInput!) { variableUpsert(input: $input) }",
        {"input": {"projectId": PROJECT, "environmentId": ENV, "serviceId": sid,
                   "name": "OLLAMA_ENABLED", "value": value}}
    )


def get_enabled(sid):
    q = f'query {{ variables(projectId: "{PROJECT}", environmentId: "{ENV}", serviceId: "{sid}") }}'
    data = _call(q)
    return data["data"]["variables"].get("OLLAMA_ENABLED", "(не задано)")


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("on", "off", "status"):
        print(__doc__)
        sys.exit(1)
    
    action = sys.argv[1]
    target = sys.argv[2].lower().replace("-bot", "") if len(sys.argv) > 2 else None
    
    targets = {target: BOTS[target]} if target else BOTS
    if target and target not in BOTS:
        print(f"Unknown bot: {target}. Available: {', '.join(BOTS)}")
        sys.exit(1)
    
    if action == "status":
        print(f"\n{'bot':<10} OLLAMA_ENABLED")
        print("-" * 30)
        for name, sid in targets.items():
            print(f"{name:<10} {get_enabled(sid)}")
        return
    
    new_val = "true" if action == "on" else "false"
    print(f"\nSetting OLLAMA_ENABLED={new_val} on {len(targets)} bot(s):")
    for name, sid in targets.items():
        try:
            ok = set_enabled(sid, new_val)["data"]["variableUpsert"]
            print(f"  {name:<10} {'✓' if ok else '✗'}")
        except Exception as e:
            print(f"  {name:<10} ✗ {e}")
    print(f"\nRailway пересоберёт затронутые сервисы за 1-2 минуты.")


if __name__ == "__main__":
    main()
