import asyncio
import json
import logging
import math
import random
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("server")

# =========================
# Конфигурация
# =========================
HOST = "0.0.0.0"
PORT = 8765
MAX_PLAYERS = 10
TICK_RATE = 20
TICK_INTERVAL = 1.0 / TICK_RATE

SPEED = 4.0              # единиц за тик (постоянное движение)
BOOST_MULT = 1.8         # множитель скорости при ускорении
TURN_SPEED = 3.5         # рад/сек — скорость поворота

# Круглый мир
WORLD_CX = 0.0
WORLD_CY = 0.0
WORLD_R = 1200.0
SPAWN_R = WORLD_R * 0.8   # спавн не у самого края

# =========================
# Состояние
# =========================
players = {}   # ws -> {"id","x","y","angle","name","turn","boost"}
next_id = 1
_last_state_str = None


# =========================
# Утилиты мира
# =========================
def clamp_to_circle(x, y):
    """Прижимает точку к границе круга, если она вышла за радиус."""
    dx = x - WORLD_CX
    dy = y - WORLD_CY
    dist = math.hypot(dx, dy)
    if dist <= WORLD_R:
        return x, y
    k = WORLD_R / dist
    return WORLD_CX + dx * k, WORLD_CY + dy * k


def spawn_position():
    """Случайная точка внутри круга спавна."""
    angle = random.uniform(0, 2 * math.pi)
    dist = random.uniform(0, SPAWN_R)
    return WORLD_CX + math.cos(angle) * dist, WORLD_CY + math.sin(angle) * dist


# =========================
# Сериализация состояния
# =========================
def build_state_str():
    """
    Одна сериализация на тик. Короткие ключи:
    i=id, x, y, a=angle, n=name, b=boost.
    """
    global _last_state_str
    state = {
        "type": "state",
        "p": [
            {
                "i": p["id"],
                "x": round(p["x"], 2),
                "y": round(p["y"], 2),
                "a": round(p["angle"], 3),
                "n": p["name"],
                "b": int(p["boost"]),
            }
            for p in players.values()
        ],
    }
    s = json.dumps(state, separators=(",", ":"))
    if s == _last_state_str:
        return None
    _last_state_str = s
    return s


async def send_json(ws, obj):
    try:
        await ws.send(json.dumps(obj, separators=(",", ":")))
    except ConnectionClosed:
        pass


async def broadcast_state(state_str):
    """Рассылка готовой строки всем; одна ошибка не ломает остальных."""
    if not state_str or not players:
        return
    await asyncio.gather(
        *[ws.send(state_str) for ws in list(players.keys())],
        return_exceptions=True,
    )


# =========================
# Обработчик подключения
# =========================
async def handler(ws):
    global next_id, _last_state_str

    if len(players) >= MAX_PLAYERS:
        await send_json(ws, {"type": "error", "message": "Сервер заполнен (10/10)"})
        await ws.close()
        log.info("Отклонено: сервер заполнен")
        return

    player_id = next_id
    next_id += 1

    sx, sy = spawn_position()
    players[ws] = {
        "id": player_id,
        "x": sx,
        "y": sy,
        "angle": random.uniform(0, 2 * math.pi),
        "name": f"Player{player_id}",
        "turn": 0,
        "boost": False,
    }
    _last_state_str = None
    log.info("Игрок %s подключился (всего: %d)", player_id, len(players))

    await send_json(ws, {
        "type": "welcome",
        "id": player_id,
        "x": players[ws]["x"],
        "y": players[ws]["y"],
        "angle": players[ws]["angle"],
        "world": {"cx": WORLD_CX, "cy": WORLD_CY, "r": WORLD_R},
    })

    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                await send_json(ws, {"type": "error", "message": "Некорректный JSON"})
                continue

            mtype = data.get("type")
            p = players.get(ws)
            if p is None:
                break

            if mtype == "join":
                name = (data.get("name") or "").strip()
                if not name:
                    await send_json(ws, {"type": "error", "message": "Пустое имя"})
                else:
                    p["name"] = name[:32]
                    _last_state_str = None
                    log.info("Игрок %s назвался: %s", p["id"], p["name"])

            elif mtype == "input":
                # turn: -1 влево, 0 прямо, +1 вправо
                if "turn" in data:
                    try:
                        t = int(data["turn"])
                        p["turn"] = -1 if t < 0 else (1 if t > 0 else 0)
                    except (TypeError, ValueError):
                        pass
                # boost: bool
                if "boost" in data:
                    p["boost"] = bool(data["boost"])

            elif mtype == "ping":
                await send_json(ws, {"type": "pong"})

            else:
                await send_json(ws, {"type": "error", "message": f"Неизвестный тип: {mtype}"})

    except ConnectionClosed:
        pass
    finally:
        removed = players.pop(ws, None)
        if removed:
            _last_state_str = None
            log.info("Игрок %s отключился (осталось: %d)", removed["id"], len(players))


# =========================
# Игровой шаг
# =========================
def step_world():
    """Постоянное движение вперёд + поворот + буст + круглая граница."""
    if not players:
        return
    dt = TICK_INTERVAL
    for p in players.values():
        # 1) поворот
        if p["turn"]:
            p["angle"] += p["turn"] * TURN_SPEED * dt
        # 2) скорость (буст)
        spd = SPEED * (BOOST_MULT if p["boost"] else 1.0)
        # 3) всегда двигаемся вперёд по углу
        p["x"] += math.cos(p["angle"]) * spd
        p["y"] += math.sin(p["angle"]) * spd
        # 4) прижимание к круглой границе
        p["x"], p["y"] = clamp_to_circle(p["x"], p["y"])


# =========================
# Игровой цикл
# =========================
async def game_loop():
    log.info("Игровой цикл запущен (%d Гц, до %d игроков)", TICK_RATE, MAX_PLAYERS)
    while True:
        step_world()
        state_str = build_state_str()
        await broadcast_state(state_str)
        await asyncio.sleep(TICK_INTERVAL)


# =========================
# Запуск
# =========================
async def main():
    async with serve(handler, HOST, PORT):
        log.info("Сервер слушает ws://%s:%d", HOST, PORT)
        log.info("Круглый мир: центр (%.0f, %.0f), радиус %.0f",
                 WORLD_CX, WORLD_CY, WORLD_R)
        await game_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Сервер остановлен")