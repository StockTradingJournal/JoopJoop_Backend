import socketio
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from game_manager import GameManager
import uvicorn
import os
import time

# Create Socket.IO server
sio = socketio.AsyncServer(
    async_mode='asgi',
    cors_allowed_origins=["*"]
)

# Create FastAPI app
app = FastAPI()

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Create game manager
game_manager = GameManager()
game_manager.set_sio(sio)

# Combine FastAPI and Socket.IO
socket_app = socketio.ASGIApp(sio, app)


@app.get("/health")
async def health_check():
    return {"status": "ok"}


@sio.event
async def connect(sid, environ):
    print(f"Client connected: {sid}")


@sio.event
async def disconnect(sid):
    print(f"Client disconnected: {sid}")
    await game_manager.handle_disconnect(sid, sio)


# ─────────────────────────────────────────────────────────
# Room management
# ─────────────────────────────────────────────────────────

@sio.event
async def create_room(sid, data):
    try:
        nickname = data.get('nickname')
        if not nickname:
            await sio.emit('room:error', {'code': 'INVALID_DATA', 'message': 'Nickname required'}, room=sid)
            return
        room_id = await game_manager.create_room(sid, nickname)
        await sio.enter_room(sid, room_id)
        await sio.emit('room:created', {'roomId': room_id}, room=sid)
        await game_manager.broadcast_state(room_id, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'CREATE_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def join_match_queue(sid, data):
    """Quick match: queue by desired player count (3–6). Room starts at item_selection when full."""
    try:
        nickname = (data or {}).get('nickname')
        player_count = (data or {}).get('playerCount')
        if not nickname:
            await sio.emit('match_queue:error', {'message': '닉네임이 필요합니다.'}, room=sid)
            return
        if player_count not in (3, 4, 5, 6):
            await sio.emit('match_queue:error', {'message': '인원은 3~6명만 선택할 수 있습니다.'}, room=sid)
            return
        ok = await game_manager.join_match_queue(sid, nickname, player_count, sio)
        if ok:
            await sio.emit('match_queue:joined', {'playerCount': player_count}, room=sid)
        else:
            await sio.emit('match_queue:error', {'message': '매칭 대기에 참여할 수 없습니다.'}, room=sid)
    except Exception as e:
        await sio.emit('match_queue:error', {'message': str(e)}, room=sid)


@sio.event
async def leave_match_queue(sid, data):
    await game_manager.leave_match_queue(sid)
    await sio.emit('match_queue:left', {}, room=sid)


@sio.event
async def join_room(sid, data):
    try:
        print(f"Client {sid}, data {data} attempting to join room")
        room_id = data.get('roomId')
        nickname = data.get('nickname')
        if not room_id or not nickname:
            await sio.emit('room:error', {'code': 'INVALID_DATA', 'message': 'Room ID and nickname required'}, room=sid)
            return
        success = await game_manager.join_room(sid, room_id, nickname)
        if success:
            await sio.enter_room(sid, room_id)
            await sio.emit('room:joined', {'roomId': room_id}, room=sid)
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {'code': 'JOIN_FAILED', 'message': 'Could not join room'}, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'JOIN_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def player_ready(sid, data):
    try:
        ready = data.get('ready', False)
        room_id = await game_manager.set_player_ready(sid, ready)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'READY_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def start_game(sid, data):
    try:
        room_id = await game_manager.start_game(sid)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {
                'code': 'START_FAILED',
                'message': '게임을 시작할 수 없습니다. 2명 이상 참여, 모든 일반 플레이어 준비 완료 필요'
            }, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'START_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def return_to_lobby(sid, data):
    """Reset room from game over to lobby (same players)."""
    try:
        room_id = await game_manager.return_to_lobby(sid)
        if not room_id:
            await sio.emit(
                'room:error',
                {'code': 'RETURN_LOBBY_FAILED', 'message': '대기실로 돌아갈 수 없습니다. 게임 종료 후에만 가능합니다.'},
                room=sid,
            )
    except Exception as e:
        await sio.emit('room:error', {'code': 'RETURN_LOBBY_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def add_bot(sid, data):
    """Host adds a bot player to the room for solo testing."""
    try:
        room_id = await game_manager.add_bot_to_room(sid)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {
                'code': 'BOT_FAILED',
                'message': '봇을 추가할 수 없습니다. 로비 상태에서 호스트만 추가 가능합니다.'
            }, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'BOT_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def leave_room(sid, data):
    try:
        room_id = game_manager.player_to_room.get(sid)
        if room_id:
            await sio.leave_room(sid, room_id)
        await game_manager.handle_disconnect(sid, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'LEAVE_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def chat_message(sid, data):
    try:
        message = data.get('message')
        if not message:
            return
        room_id = game_manager.player_to_room.get(sid)
        if not room_id or room_id not in game_manager.rooms:
            return
        room = game_manager.rooms[room_id]
        player = room.players.get(sid)
        if not player:
            return
        chat_data = {
            'playerId': sid,
            'nickname': player.nickname,
            'message': message,
            'timestamp': int(time.time() * 1000)
        }
        await sio.emit('chat:message', chat_data, room=room_id)
    except Exception as e:
        print(f"Chat message error: {e}")


# ─────────────────────────────────────────────────────────
# Phase 1 actions
# ─────────────────────────────────────────────────────────

@sio.event
async def place_bid(sid, data):
    try:
        amount = data.get('amount')
        if amount is None:
            await sio.emit('room:error', {'code': 'INVALID_BID', 'message': 'Bid amount required'}, room=sid)
            return
        room_id = await game_manager.handle_bid(sid, amount)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'BID_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def pass_turn(sid, data):
    try:
        room_id = await game_manager.handle_pass(sid)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'PASS_FAILED', 'message': str(e)}, room=sid)


# ─────────────────────────────────────────────────────────
# Phase 2 actions
# ─────────────────────────────────────────────────────────

@sio.event
async def play_card(sid, data):
    try:
        card_id = data.get('card_id')
        if card_id is None:
            await sio.emit('room:error', {'code': 'INVALID_CARD', 'message': 'Card ID required'}, room=sid)
            return
        room_id = await game_manager.handle_play_card(sid, card_id)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
    except Exception as e:
        await sio.emit('room:error', {'code': 'PLAY_FAILED', 'message': str(e)}, room=sid)


# ─────────────────────────────────────────────────────────
# Item event handlers
# ─────────────────────────────────────────────────────────

@sio.event
async def select_item(sid, data):
    """Player selects their special item before Phase 1."""
    try:
        item = data.get('item')
        if item not in ('reroll', 'peek', 'reverse'):
            await sio.emit('room:error', {'code': 'INVALID_ITEM', 'message': '유효하지 않은 아이템입니다.'}, room=sid)
            return
        room_id = await game_manager.handle_select_item(sid, item)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': '아이템을 선택할 수 없습니다.'}, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def use_item_reroll(sid, data):
    """Use reroll item to reshuffle current round's cards."""
    try:
        room_id = await game_manager.handle_use_item_reroll(sid)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': '리롤 아이템을 사용할 수 없습니다.'}, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def use_item_peek(sid, data):
    """Use peek item to see a target player's coins or real estate cards."""
    try:
        target_id = data.get('targetId')
        if not target_id:
            await sio.emit('room:error', {'code': 'INVALID_DATA', 'message': '대상 플레이어를 지정해주세요.'}, room=sid)
            return
        room_id = await game_manager.handle_use_item_peek(sid, target_id)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': '엿보기 아이템을 사용할 수 없습니다.'}, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': str(e)}, room=sid)


@sio.event
async def use_item_reverse(sid, data):
    """Use reverse item to flip turn direction. Requires bid after use."""
    try:
        room_id = await game_manager.handle_use_item_reverse(sid)
        if room_id:
            await game_manager.broadcast_state(room_id, sio)
        else:
            await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': '리버스 아이템을 사용할 수 없습니다.'}, room=sid)
    except Exception as e:
        await sio.emit('room:error', {'code': 'ITEM_FAILED', 'message': str(e)}, room=sid)


if __name__ == "__main__":
    port = int(os.getenv("PORT", 8000))
    uvicorn.run(socket_app, host="0.0.0.0", port=port)
