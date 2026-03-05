import random
import string
import asyncio
import time
from typing import Dict, List, Optional, Any
from enum import Enum


class GamePhase(Enum):
    LOBBY = "lobby"
    ITEM_SELECTION = "item_selection"
    PHASE1_BIDDING = "phase1_bidding"
    PHASE2_PLAYING = "phase2_playing"
    GAME_OVER = "game_over"


class Player:
    def __init__(self, sid: str, nickname: str):
        self.sid = sid
        self.nickname = nickname
        self.ready = False
        self.is_bot = False
        self.coins = 15000              # starting funds
        self.properties: List[int] = []        # job cards won in Phase 1
        self.real_estate_cards: List[int] = [] # real estate cards won in Phase 2
        self.current_bid = 0
        self.has_passed = False
        self.selected_property: Optional[int] = None  # Phase 2 job card chosen this round
        self.selected_item: Optional[str] = None      # 'reroll' | 'peek' | 'reverse'
        self.item_used = False


class Room:
    def __init__(self, room_id: str, creator_sid: str, creator_nickname: str):
        self.room_id = room_id
        self.players: Dict[str, Player] = {}
        self.phase = GamePhase.LOBBY

        # Decks
        self.job_deck: List[int] = list(range(1, 31))       # job cards 1-30
        self.real_estate_deck: List[int] = list(range(1, 16))  # real estate cards 1-15

        # Table cards
        self.current_properties: List[int] = []           # job cards on table (Phase 1)
        self.current_real_estate_cards: List[int] = []    # real estate on table (Phase 2)

        # Phase 1 bidding state
        self.current_bid = 0
        self.current_high_bidder: Optional[str] = None
        self.turn_order: List[str] = []   # fixed random order, set once
        self.current_turn_index = 0
        self.turn_direction = 1           # 1=forward, -1=reverse
        self.round_number = 1
        self.turn_timer_task: Optional[asyncio.Task] = None
        self.turn_timeout = 10            # seconds per turn
        self.turn_start_time: float = 0.0

        # Phase 2 state
        self.phase2_selections: Dict[str, int] = {}   # {player_sid: job_card_value}
        self.phase2_round_number = 1
        self.phase2_timer_task: Optional[asyncio.Task] = None
        self.phase2_start_time: float = 0.0

        # Item state
        self.reverse_used_this_round = False
        self.must_bid_player: Optional[str] = None  # sid who used reverse (must bid)
        self.item_selection_count = 0
        self._peek_result: Optional[Dict] = None    # cleared after each broadcast

        # Round tracking
        self.last_round_winner_sid: Optional[str] = None  # who won highest card last round

        # Phase 2 separate timeout
        self.phase2_timeout = 10          # seconds for Phase 2 card selection
        self.phase2_resolving = False     # guard against double-resolve

        # Last pass event (cleared after each broadcast)
        self._last_pass_event: Optional[Dict] = None

        # Round result (cleared after each broadcast)
        self._round_result: Optional[Dict] = None

        # Add creator
        creator = Player(creator_sid, creator_nickname)
        self.players[creator_sid] = creator

        # Shuffle decks at room creation
        random.shuffle(self.job_deck)
        random.shuffle(self.real_estate_deck)


class GameManager:
    def __init__(self):
        self.rooms: Dict[str, Room] = {}
        self.player_to_room: Dict[str, str] = {}
        self.sio = None

    def set_sio(self, sio):
        self.sio = sio

    def _generate_room_id(self) -> str:
        return ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))

    # ─────────────────────────────────────────────────────────
    # Room management
    # ─────────────────────────────────────────────────────────

    async def create_room(self, sid: str, nickname: str) -> str:
        room_id = self._generate_room_id()
        while room_id in self.rooms:
            room_id = self._generate_room_id()
        room = Room(room_id, sid, nickname)
        self.rooms[room_id] = room
        self.player_to_room[sid] = room_id
        return room_id

    async def add_bot_to_room(self, host_sid: str) -> Optional[str]:
        """Add a bot player to the room. Host only, lobby only, max 6 players."""
        room_id = self.player_to_room.get(host_sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.LOBBY:
            return None
        first_player_sid = next(iter(room.players.keys()))
        if host_sid != first_player_sid:
            return None
        if len(room.players) >= 6:
            return None
        bot_num = sum(1 for p in room.players.values() if p.is_bot) + 1
        bot_sid = f"bot_{room_id}_{bot_num}"
        bot_nickname = f"봇{bot_num}"
        bot = Player(bot_sid, bot_nickname)
        bot.is_bot = True
        bot.ready = True
        room.players[bot_sid] = bot
        self.player_to_room[bot_sid] = room_id
        print(f"🤖 Bot {bot_nickname} ({bot_sid}) added to room {room_id}")
        return room_id

    async def join_room(self, sid: str, room_id: str, nickname: str) -> bool:
        if room_id not in self.rooms:
            return False
        room = self.rooms[room_id]
        if len(room.players) >= 6:
            return False
        if room.phase != GamePhase.LOBBY:
            return False
        player = Player(sid, nickname)
        room.players[sid] = player
        self.player_to_room[sid] = room_id
        return True

    async def set_player_ready(self, sid: str, ready: bool) -> Optional[str]:
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if sid in room.players:
            room.players[sid].ready = ready
        return room_id

    async def start_game(self, sid: str) -> Optional[str]:
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if len(room.players) < 2:
            return None
        first_player_sid = next(iter(room.players.keys()))
        if sid != first_player_sid:
            return None
        non_host = [p for s, p in room.players.items() if s != first_player_sid]
        if not all(p.ready for p in non_host):
            return None
        # Move to item selection phase
        room.phase = GamePhase.ITEM_SELECTION
        room.item_selection_count = 0
        print(f"🎮 Game started in room {room_id}, moving to item selection")
        # Schedule bot item selections after a short delay
        asyncio.create_task(self._trigger_bot_item_selections(room))
        return room_id

    # ─────────────────────────────────────────────────────────
    # Item selection
    # ─────────────────────────────────────────────────────────

    async def handle_select_item(self, sid: str, item: str) -> Optional[str]:
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.ITEM_SELECTION:
            return None
        if item not in ('reroll', 'peek', 'reverse'):
            return None
        player = room.players.get(sid)
        if not player or player.selected_item is not None:
            return None
        player.selected_item = item
        room.item_selection_count += 1
        print(f"🎁 {player.nickname} selected item: {item} ({room.item_selection_count}/{len(room.players)})")
        # If all players have selected, start Phase 1
        if room.item_selection_count >= len(room.players):
            if self.sio:
                await self.broadcast_state(room_id, self.sio)
            await asyncio.sleep(1)
            await self._start_phase1(room)
        return room_id

    # ─────────────────────────────────────────────────────────
    # Phase 1: Job card auction
    # ─────────────────────────────────────────────────────────

    async def _start_phase1(self, room: Room):
        print(f"▶️ Starting Phase 1 round {room.round_number}")
        room.phase = GamePhase.PHASE1_BIDDING
        # Fix turn order once at the beginning of the game
        if not room.turn_order:
            room.turn_order = list(room.players.keys())
            random.shuffle(room.turn_order)
            print(f"🔀 Fixed turn order: {[room.players[s].nickname for s in room.turn_order]}")
        # Round 2+: previous winner starts; round 1: index 0
        if room.last_round_winner_sid and room.last_round_winner_sid in room.turn_order:
            room.current_turn_index = room.turn_order.index(room.last_round_winner_sid)
            print(f"🎯 Round {room.round_number} starts from last winner: {room.players[room.last_round_winner_sid].nickname}")
        else:
            room.current_turn_index = 0
        # Deal job cards equal to number of players (sorted ascending on table)
        num_players = len(room.players)
        room.current_properties = sorted(room.job_deck[:num_players])
        room.job_deck = room.job_deck[num_players:]
        # Reset round state
        room.current_bid = 0
        room.current_high_bidder = None
        room.reverse_used_this_round = False
        room.must_bid_player = None
        for player in room.players.values():
            player.current_bid = 0
            player.has_passed = False
        print(f"📋 Dealt job cards: {room.current_properties}, remaining deck: {len(room.job_deck)}")
        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await self._check_auto_pass_and_start_timer(room)

    async def _check_auto_pass_and_start_timer(self, room: Room):
        """Auto-pass bots that can't afford minimum bid; human players just get the timer."""
        if not room.turn_order or room.current_turn_index >= len(room.turn_order):
            return
        current_sid = room.turn_order[room.current_turn_index]
        player = room.players.get(current_sid)
        if player and not player.has_passed and player.is_bot:
            min_bid = room.current_bid + 1000
            if (player.coins + player.current_bid) < min_bid:
                print(f"💸 Bot {player.nickname} can't afford minimum bid, auto-passing")
                await self.handle_pass(current_sid)
                if self.sio:
                    await self.broadcast_state(room.room_id, self.sio)
                return
        await self._start_turn_timer(room)

    def _get_next_active_index(self, room: Room) -> Optional[int]:
        """Return index of next non-passed player, respecting turn_direction."""
        num = len(room.turn_order)
        idx = room.current_turn_index
        for _ in range(num):
            idx = (idx + room.turn_direction + num) % num
            sid = room.turn_order[idx]
            if sid in room.players and not room.players[sid].has_passed:
                return idx
        return None

    async def _start_turn_timer(self, room: Room):
        if room.turn_timer_task:
            room.turn_timer_task.cancel()
        room.turn_start_time = time.time()

        async def timer_expired():
            try:
                await asyncio.sleep(room.turn_timeout)
                # Clear self-reference before doing any work so that
                # _cancel_turn_timer() calls inside handle_pass / handle_bid
                # do NOT cancel this coroutine.
                room.turn_timer_task = None
                if room.phase == GamePhase.PHASE1_BIDDING and room.turn_order:
                    current_sid = room.turn_order[room.current_turn_index]
                    player = room.players.get(current_sid)
                    if player and not player.has_passed:
                        print(f"⏰ Timer expired for {player.nickname}")
                        if room.must_bid_player == current_sid:
                            # Must bid - force minimum bid or pass if broke
                            min_bid = room.current_bid + 1000
                            if (player.coins + player.current_bid) >= min_bid:
                                await self.handle_bid(current_sid, min_bid)
                            else:
                                room.must_bid_player = None
                                await self.handle_pass(current_sid)
                        else:
                            await self.handle_pass(current_sid)
                        if self.sio:
                            await self.broadcast_state(room.room_id, self.sio)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Phase 1 timer error: {e}")

        room.turn_timer_task = asyncio.create_task(timer_expired())
        # If current player is a bot, schedule their action
        asyncio.create_task(self._delayed_bot_phase1_action(room))

    def _cancel_turn_timer(self, room: Room):
        if room.turn_timer_task:
            room.turn_timer_task.cancel()
            room.turn_timer_task = None

    async def handle_bid(self, sid: str, amount: int) -> Optional[str]:
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.PHASE1_BIDDING:
            return None
        if not room.turn_order or room.turn_order[room.current_turn_index] != sid:
            return None
        player = room.players[sid]
        if player.has_passed:
            return None
        if amount <= room.current_bid:
            return None
        # Check affordability: coins (already net of previous bid) + current_bid >= new amount
        if (player.coins + player.current_bid) < amount:
            return None

        # Deduct incremental cost immediately
        player.coins -= (amount - player.current_bid)
        player.current_bid = amount
        room.current_bid = amount
        room.current_high_bidder = sid
        # Clear must_bid after successful bid
        if room.must_bid_player == sid:
            room.must_bid_player = None
        print(f"💰 {player.nickname} bid {amount} (coins left: {player.coins})")

        active_players = [p for p in room.players.values() if not p.has_passed]
        if len(active_players) == 1:
            self._cancel_turn_timer(room)
            self._give_highest_card_to_winner(room, active_players[0])
            await self._end_phase1_round(room)
        else:
            next_idx = self._get_next_active_index(room)
            if next_idx is not None:
                room.current_turn_index = next_idx
                await self._check_auto_pass_and_start_timer(room)
            else:
                self._cancel_turn_timer(room)
                await self._end_phase1_round(room)
        return room_id

    def _give_highest_card_to_winner(self, room: Room, player: Player):
        """Last standing player wins the highest job card (bid already deducted)."""
        if room.current_properties:
            highest = max(room.current_properties)
            room.current_properties.remove(highest)
            player.properties.append(highest)
            paid = player.current_bid  # amount already deducted from coins
            player.current_bid = 0
            room.last_round_winner_sid = player.sid
            room._round_result = {
                'winnerId': player.sid,
                'winnerNickname': player.nickname,
                'wonCard': highest,
                'paid': paid,
                'refunded': 0,
            }
            print(f"🏆 {player.nickname} wins job card {highest} (paid {paid})")

    async def handle_pass(self, sid: str) -> Optional[str]:
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.PHASE1_BIDDING:
            return None
        if not room.turn_order or room.turn_order[room.current_turn_index] != sid:
            return None
        player = room.players[sid]
        if player.has_passed:
            return None
        # Block pass if player used reverse (must bid this turn)
        if room.must_bid_player == sid:
            return None

        player.has_passed = True
        # Take lowest job card from table
        acquired_card = None
        if room.current_properties:
            lowest = min(room.current_properties)
            room.current_properties.remove(lowest)
            player.properties.append(lowest)
            acquired_card = lowest
        # Refund half of current bid (floored to 1000)
        paid = player.current_bid
        refund = 0
        if player.current_bid > 0:
            refund = (player.current_bid // 2 // 1000) * 1000
            player.coins += refund
            print(f"🚫 {player.nickname} passed with bid {player.current_bid}, refunded {refund}")
        else:
            print(f"🚫 {player.nickname} passed with no bid")
        player.current_bid = 0

        # Record pass event for broadcast
        room._last_pass_event = {
            'playerId': sid,
            'nickname': player.nickname,
            'acquiredCard': acquired_card,
            'paid': paid,
            'refunded': refund,
        }

        active_players = [p for p in room.players.values() if not p.has_passed]
        if len(active_players) == 1:
            self._cancel_turn_timer(room)
            self._give_highest_card_to_winner(room, active_players[0])
            await self._end_phase1_round(room)
        elif len(active_players) == 0:
            self._cancel_turn_timer(room)
            await self._end_phase1_round(room)
        else:
            next_idx = self._get_next_active_index(room)
            if next_idx is not None:
                room.current_turn_index = next_idx
                await self._check_auto_pass_and_start_timer(room)
            else:
                self._cancel_turn_timer(room)
                await self._end_phase1_round(room)
        return room_id

    async def _end_phase1_round(self, room: Room):
        print(f"🏁 Phase 1 round {room.round_number} ended | Job deck remaining: {len(room.job_deck)}")
        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await asyncio.sleep(4)
        num_players = len(room.players)
        if len(room.job_deck) >= num_players:
            room.round_number += 1
            await self._start_phase1(room)
            if self.sio:
                await self.broadcast_state(room.room_id, self.sio)
        else:
            print("🎯 Moving to Phase 2")
            await self._start_phase2(room)

    # ─────────────────────────────────────────────────────────
    # Item handlers
    # ─────────────────────────────────────────────────────────

    async def handle_use_item_reroll(self, sid: str) -> Optional[str]:
        """Reroll the cards currently on the table."""
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        player = room.players.get(sid)
        if not player or player.selected_item != 'reroll' or player.item_used:
            return None

        if room.phase == GamePhase.PHASE1_BIDDING:
            # Only usable on first turn (no bids, no passes yet) and must be current player
            if room.current_bid > 0 or any(p.has_passed for p in room.players.values()):
                return None
            if room.turn_order[room.current_turn_index] != sid:
                return None
            num_players = len(room.players)
            combined = room.current_properties + room.job_deck
            random.shuffle(combined)
            room.current_properties = sorted(combined[:num_players])
            room.job_deck = combined[num_players:]
            player.item_used = True
            print(f"🔄 {player.nickname} used Reroll in Phase 1: {room.current_properties}")
            return room_id

        elif room.phase == GamePhase.PHASE2_PLAYING:
            # Only usable before submitting a card this round
            if player.selected_property is not None:
                return None
            num_players = len(room.players)
            combined = room.current_real_estate_cards + room.real_estate_deck
            random.shuffle(combined)
            room.current_real_estate_cards = sorted(combined[:num_players], reverse=True)
            room.real_estate_deck = combined[num_players:]
            player.item_used = True
            print(f"🔄 {player.nickname} used Reroll in Phase 2: {room.current_real_estate_cards}")
            return room_id

        return None

    async def handle_use_item_peek(self, sid: str, target_sid: str) -> Optional[str]:
        """Peek at target's coins (Phase 1) or real estate cards (Phase 2)."""
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        player = room.players.get(sid)
        if not player or player.selected_item != 'peek' or player.item_used:
            return None
        target = room.players.get(target_sid)
        if not target:
            return None

        if room.phase == GamePhase.PHASE1_BIDDING:
            if room.turn_order[room.current_turn_index] != sid:
                return None
            player.item_used = True
            room._peek_result = {
                'requesterId': sid,
                'targetId': target_sid,
                'targetNickname': target.nickname,
                'money': target.coins,
            }
            print(f"👁️ {player.nickname} peeked at {target.nickname}'s money: {target.coins}")
            return room_id

        elif room.phase == GamePhase.PHASE2_PLAYING:
            player.item_used = True
            room._peek_result = {
                'requesterId': sid,
                'targetId': target_sid,
                'targetNickname': target.nickname,
                'realEstateCards': target.real_estate_cards[:],
            }
            print(f"👁️ {player.nickname} peeked at {target.nickname}'s real estate: {target.real_estate_cards}")
            return room_id

        return None

    async def handle_use_item_reverse(self, sid: str) -> Optional[str]:
        """Reverse turn direction permanently. Only Phase 1, must be your turn. Must bid after."""
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.PHASE1_BIDDING:
            return None
        if not room.turn_order or room.turn_order[room.current_turn_index] != sid:
            return None
        player = room.players.get(sid)
        if not player or player.selected_item != 'reverse' or player.item_used:
            return None
        if room.reverse_used_this_round:
            return None  # One reverse per round

        room.turn_direction = -room.turn_direction
        room.reverse_used_this_round = True
        room.must_bid_player = sid
        player.item_used = True
        print(f"🔀 {player.nickname} used Reverse! Direction: {room.turn_direction}")
        return room_id

    # ─────────────────────────────────────────────────────────
    # Phase 2: Real estate auction
    # ─────────────────────────────────────────────────────────

    async def _start_phase2(self, room: Room):
        print("▶️ Starting Phase 2")
        room.phase = GamePhase.PHASE2_PLAYING
        room.phase2_round_number = 1
        num_players = len(room.players)
        room.current_real_estate_cards = sorted(
            room.real_estate_deck[:num_players], reverse=True
        )
        room.real_estate_deck = room.real_estate_deck[num_players:]
        room.phase2_selections = {}
        for player in room.players.values():
            player.selected_property = None
        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await self._start_phase2_timer(room)

    async def _start_phase2_timer(self, room: Room):
        if room.phase2_timer_task:
            room.phase2_timer_task.cancel()
        room.phase2_start_time = time.time()
        room.phase2_resolving = False

        async def timer_expired():
            try:
                await asyncio.sleep(room.phase2_timeout)
                # Clear self-reference before doing any work so that
                # _cancel_phase2_timer() inside _check_phase2_all_selected
                # does NOT cancel this coroutine.
                room.phase2_timer_task = None
                if room.phase == GamePhase.PHASE2_PLAYING:
                    print("⏰ Phase 2 timer expired, auto-selecting lowest job cards")
                    await self._auto_select_unselected(room)
                    if self.sio:
                        await self.broadcast_state(room.room_id, self.sio)
            except asyncio.CancelledError:
                pass
            except Exception as e:
                print(f"Phase 2 timer error: {e}")

        room.phase2_timer_task = asyncio.create_task(timer_expired())
        # Schedule bot card selections for this round
        asyncio.create_task(self._schedule_bot_phase2_actions(room))

    def _cancel_phase2_timer(self, room: Room):
        if room.phase2_timer_task:
            room.phase2_timer_task.cancel()
            room.phase2_timer_task = None

    async def _auto_select_unselected(self, room: Room):
        """Auto-select lowest job card for players who haven't chosen."""
        for sid, player in room.players.items():
            if sid not in room.phase2_selections and player.properties:
                lowest = min(player.properties)
                player.selected_property = lowest
                room.phase2_selections[sid] = lowest
                print(f"⏰ Auto-selected job card {lowest} for {player.nickname}")
        await self._check_phase2_all_selected(room)

    async def handle_play_card(self, sid: str, card_id: int) -> Optional[str]:
        """Player submits a job card for Phase 2 real estate auction."""
        room_id = self.player_to_room.get(sid)
        if not room_id or room_id not in self.rooms:
            return None
        room = self.rooms[room_id]
        if room.phase != GamePhase.PHASE2_PLAYING:
            return None
        player = room.players[sid]
        if card_id not in player.properties:
            return None
        if player.selected_property is not None:
            return None
        player.selected_property = card_id
        room.phase2_selections[sid] = card_id
        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await self._check_phase2_all_selected(room)
        return room_id

    async def _check_phase2_all_selected(self, room: Room):
        """If all players submitted (or have no cards), resolve the round."""
        if room.phase2_resolving:
            return
        all_submitted = all(
            (sid in room.phase2_selections or len(p.properties) == 0)
            for sid, p in room.players.items()
        )
        if not all_submitted:
            return
        room.phase2_resolving = True
        self._cancel_phase2_timer(room)
        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await asyncio.sleep(2)
        await self._resolve_phase2(room)

    async def _resolve_phase2(self, room: Room):
        """Distribute real estate cards: highest job card → highest real estate."""
        submissions = sorted(room.phase2_selections.items(), key=lambda x: x[1], reverse=True)
        sorted_estates = sorted(room.current_real_estate_cards, reverse=True)

        for i, (sid, job_card) in enumerate(submissions):
            player = room.players[sid]
            if job_card in player.properties:
                player.properties.remove(job_card)
            if i < len(sorted_estates):
                player.real_estate_cards.append(sorted_estates[i])
                print(f"🏠 {player.nickname} (job {job_card}) → real estate {sorted_estates[i]}")

        room.phase2_selections = {}
        for player in room.players.values():
            player.selected_property = None

        if self.sio:
            await self.broadcast_state(room.room_id, self.sio)
        await asyncio.sleep(2)

        num_players = len(room.players)
        has_job_cards = any(len(p.properties) > 0 for p in room.players.values())
        enough_estates = len(room.real_estate_deck) >= num_players

        if has_job_cards and enough_estates:
            room.phase2_round_number += 1
            room.phase2_resolving = False
            room.current_real_estate_cards = sorted(
                room.real_estate_deck[:num_players], reverse=True
            )
            room.real_estate_deck = room.real_estate_deck[num_players:]
            if self.sio:
                await self.broadcast_state(room.room_id, self.sio)
            await self._start_phase2_timer(room)
        else:
            print("🏆 Game Over!")
            room.phase = GamePhase.GAME_OVER
            room.phase2_resolving = False
            if self.sio:
                await self.broadcast_state(room.room_id, self.sio)

    def _calculate_final_scores(self, room: Room) -> List[Dict[str, Any]]:
        rankings = []
        for sid, player in room.players.items():
            estate_value = sum(c * 1000 for c in player.real_estate_cards)
            final_score = player.coins + estate_value
            rankings.append({
                'playerId': sid,
                'nickname': player.nickname,
                'estateValue': estate_value,
                'realEstateCards': player.real_estate_cards,
                'remainingCoins': player.coins,
                'finalScore': final_score,
            })
        rankings.sort(key=lambda x: x['finalScore'], reverse=True)
        for i, r in enumerate(rankings):
            r['rank'] = i + 1
        return rankings

    # ─────────────────────────────────────────────────────────
    # Disconnect handling
    # ─────────────────────────────────────────────────────────

    async def handle_disconnect(self, sid: str, sio):
        room_id = self.player_to_room.get(sid)
        if room_id and room_id in self.rooms:
            room = self.rooms[room_id]
            first_player_sid = next(iter(room.players.keys())) if room.players else None
            is_host_leaving = sid == first_player_sid
            if sid in room.players:
                will_be_empty = len(room.players) <= 1
                del room.players[sid]
                if will_be_empty or is_host_leaving:
                    await sio.emit(
                        'room:destroyed',
                        {'message': '호스트가 나가서 방이 파괴되었습니다.'},
                        room=room_id
                    )
                    del self.rooms[room_id]
                else:
                    await self.broadcast_state(room_id, sio)
        if sid in self.player_to_room:
            del self.player_to_room[sid]

    # ─────────────────────────────────────────────────────────
    # Bot logic
    # ─────────────────────────────────────────────────────────

    async def _trigger_bot_item_selections(self, room: Room):
        """Schedule item selection for all bot players with individual delays."""
        for sid, player in list(room.players.items()):
            if player.is_bot and player.selected_item is None:
                delay = random.uniform(1.0, 3.0)
                asyncio.create_task(self._delayed_bot_item_selection(room, sid, delay))

    async def _delayed_bot_item_selection(self, room: Room, bot_sid: str, delay: float):
        """After a delay, bot randomly picks an item."""
        await asyncio.sleep(delay)
        if room.phase != GamePhase.ITEM_SELECTION:
            return
        player = room.players.get(bot_sid)
        if not player or not player.is_bot or player.selected_item is not None:
            return
        item = random.choice(['reroll', 'peek', 'reverse'])
        room_id = await self.handle_select_item(bot_sid, item)
        if room_id and self.sio:
            await self.broadcast_state(room_id, self.sio)

    async def _delayed_bot_phase1_action(self, room: Room):
        """If the current turn player is a bot, wait briefly then act."""
        if not room.turn_order or room.current_turn_index >= len(room.turn_order):
            return
        bot_sid = room.turn_order[room.current_turn_index]
        player = room.players.get(bot_sid)
        if not player or not player.is_bot or player.has_passed:
            return

        delay = random.uniform(1.0, 2.5)
        await asyncio.sleep(delay)

        # Re-validate: phase and turn may have changed during sleep
        if room.phase != GamePhase.PHASE1_BIDDING:
            return
        if not room.turn_order or room.turn_order[room.current_turn_index] != bot_sid:
            return
        player = room.players.get(bot_sid)
        if not player or not player.is_bot or player.has_passed:
            return

        min_bid = room.current_bid + 1000
        can_afford = (player.coins + player.current_bid) >= min_bid
        must_bid = room.must_bid_player == bot_sid

        if not can_afford:
            return  # auto-pass will handle affordability

        if not must_bid and random.random() < 0.4:
            # Bot decides to pass
            room_id = await self.handle_pass(bot_sid)
            if room_id and self.sio:
                await self.broadcast_state(room_id, self.sio)
        else:
            # Bot decides to bid: minimum bid + small random increment
            max_affordable = player.coins + player.current_bid
            extra = random.choice([0, 1000, 2000, 3000])
            bid_amount = min(min_bid + extra, max_affordable)
            bid_amount = (bid_amount // 1000) * 1000
            if bid_amount < min_bid:
                bid_amount = min_bid
            if bid_amount > max_affordable:
                bid_amount = (max_affordable // 1000) * 1000
            if bid_amount < min_bid:
                # Can't make a valid bid after rounding; pass instead
                room_id = await self.handle_pass(bot_sid)
            else:
                room_id = await self.handle_bid(bot_sid, bid_amount)
            if room_id and self.sio:
                await self.broadcast_state(room_id, self.sio)

    async def _schedule_bot_phase2_actions(self, room: Room):
        """Schedule card selection for all bots in the current Phase 2 round."""
        for sid, player in list(room.players.items()):
            if player.is_bot and player.properties and player.selected_property is None:
                delay = random.uniform(1.0, 5.0)
                asyncio.create_task(self._delayed_bot_phase2_action(room, sid, delay))

    async def _delayed_bot_phase2_action(self, room: Room, bot_sid: str, delay: float):
        """After a delay, bot plays a random job card in Phase 2."""
        await asyncio.sleep(delay)
        if room.phase != GamePhase.PHASE2_PLAYING:
            return
        player = room.players.get(bot_sid)
        if not player or not player.is_bot or not player.properties or player.selected_property is not None:
            return
        card_id = random.choice(player.properties)
        room_id = await self.handle_play_card(bot_sid, card_id)
        if room_id and self.sio:
            await self.broadcast_state(room_id, self.sio)

    # ─────────────────────────────────────────────────────────
    # State broadcast
    # ─────────────────────────────────────────────────────────

    async def broadcast_state(self, room_id: str, sio):
        if room_id not in self.rooms:
            return
        room = self.rooms[room_id]
        first_player_sid = next(iter(room.players.keys())) if room.players else None

        all_selected = (
            len(room.phase2_selections) == len(room.players)
            if room.phase == GamePhase.PHASE2_PLAYING
            else False
        )

        # Player items info (visible to all)
        player_items: Dict[str, Dict] = {}
        for sid, p in room.players.items():
            player_items[sid] = {'item': p.selected_item, 'used': p.item_used}

        final_rankings = None
        if room.phase == GamePhase.GAME_OVER:
            final_rankings = self._calculate_final_scores(room)

        # Collect peek result and clear it
        peek_result = room._peek_result
        room._peek_result = None

        # Collect pass event and clear it
        last_pass_event = room._last_pass_event
        room._last_pass_event = None

        # Collect round result and clear it
        round_result = room._round_result
        room._round_result = None

        player_sids = list(room.players.keys())
        for viewer_sid in player_sids:
            players_list = []
            for sid, p in room.players.items():
                is_me = sid == viewer_sid
                selected_prop = p.selected_property if (is_me or all_selected) else None

                players_list.append({
                    'id': sid,
                    'nickname': p.nickname,
                    'avatar': '👤',
                    'isReady': p.ready,
                    'isHost': sid == first_player_sid,
                    # Coins: show own, hide others'
                    'coins': p.coins if is_me else -1,
                    'propertyCount': len(p.properties),
                    'properties': p.properties if is_me else [],   # job cards (own only)
                    'realEstateCount': len(p.real_estate_cards),
                    'realEstateCards': p.real_estate_cards if is_me else [],  # own only
                    'currentBid': p.current_bid,
                    'hasPassed': p.has_passed,
                    'isCurrentTurn': (
                        room.phase == GamePhase.PHASE1_BIDDING
                        and bool(room.turn_order)
                        and room.current_turn_index < len(room.turn_order)
                        and room.turn_order[room.current_turn_index] == sid
                    ),
                    'selectedProperty': selected_prop,
                    'hasSelected': p.selected_property is not None,
                })

            # Peek result only sent to requester
            viewer_peek = peek_result if (peek_result and peek_result.get('requesterId') == viewer_sid) else None

            state = {
                'roomId': room_id,
                'gameState': 'lobby' if room.phase == GamePhase.LOBBY else 'playing',
                'phase': room.phase.value,
                'players': players_list,
                # Phase 1 table
                'currentProperties': room.current_properties,
                # Phase 2 table
                'currentRealEstateCards': room.current_real_estate_cards,
                'currentBid': room.current_bid,
                'currentHighBidder': room.current_high_bidder,
                'currentTurn': (
                    room.turn_order[room.current_turn_index]
                    if (room.phase == GamePhase.PHASE1_BIDDING and room.turn_order
                        and room.current_turn_index < len(room.turn_order))
                    else None
                ),
                'roundNumber': room.round_number,
                'phase2RoundNumber': room.phase2_round_number,
                'allPlayersSelected': all_selected,
                # Item state
                'playerItems': player_items,
                'reverseUsedThisRound': room.reverse_used_this_round,
                'turnDirection': room.turn_direction,
                'mustBidPlayer': room.must_bid_player,
                # Timers
                'turnStartTime': room.turn_start_time,
                'phase2StartTime': room.phase2_start_time,
                'turnTimeout': room.turn_timeout,
                'phase2Timeout': room.phase2_timeout,
                # Peek (private)
                'peekResult': viewer_peek,
                # Last pass event (broadcast to all)
                'lastPassEvent': last_pass_event,
                # Round result (broadcast to all, one tick)
                'roundResult': round_result,
                # Final results
                'finalRankings': final_rankings,
            }
            await sio.emit('room:state', state, room=viewer_sid)
