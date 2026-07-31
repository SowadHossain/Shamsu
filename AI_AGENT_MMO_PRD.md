# Product Requirements Document: Eidolon Realms

## 1. Product Summary

Eidolon Realms is a fantasy MMORPG-style simulation game inspired by large persistent online worlds like World of Warcraft, but designed specifically for small and large populations of AI agents. Human players may optionally observe, command, or join the world, but the main product goal is to create a living game world where AI agents can explore, form parties, fight monsters, gather resources, trade, craft, complete quests, govern settlements, raid bosses, and develop long-term reputations.

The game must support two operating modes:

- Small-agent mode: 5 to 50 AI agents running locally or on one machine.
- Large-agent mode: 500 to 10,000 AI agents running through distributed simulation workers.

The first implementation should prioritize a playable local prototype with deterministic simulation, visible world state, agent memory, questing, combat, trading, and guild behavior. The architecture must be able to scale later without rewriting core game logic.

## 2. Vision

The world should feel alive even when no human is controlling it. AI agents should not merely stand around waiting for commands. They should have goals, needs, personalities, memories, faction loyalties, economic incentives, combat abilities, and social relationships.

The product is successful when a user can start the simulation, watch agents organize themselves into parties, travel to dangerous areas, complete quests, recover from failures, trade loot, improve equipment, and make decisions that are understandable from logs and replays.

## 3. Target Users

### Primary Users

- AI researchers testing multi-agent planning.
- Game developers prototyping autonomous NPC worlds.
- Simulation hobbyists who want emergent fantasy societies.
- Streamers or creators who want watchable AI-driven stories.

### Secondary Users

- Human players who want to play alongside AI companions.
- Educators demonstrating complex systems.
- Developers benchmarking agent memory, planning, and coordination.

## 4. Non-Goals

- Do not build a full commercial MMORPG in version 1.
- Do not require cloud hosting for the local prototype.
- Do not require real-money transactions.
- Do not implement photorealistic graphics.
- Do not implement human-scale anti-cheat in version 1.
- Do not depend on proprietary game assets.
- Do not require every AI agent to call an LLM every tick.

## 5. Core Game Pillars

1. Persistent fantasy world

The world continues to change as agents act. Zones, settlements, monsters, resources, factions, and markets all have state.

2. Autonomous AI agents

Agents have goals, roles, memory, inventory, abilities, relationships, and decision loops.

3. Group coordination

Agents can form parties, guilds, raids, trade caravans, patrols, and settlement councils.

4. Explainable behavior

Every meaningful agent decision should be inspectable through logs, timelines, and replay views.

5. Scalable simulation

The same rules should support small local demos and larger distributed simulations.

6. Game-like fun

Combat, loot, leveling, quests, dungeons, crafting, and exploration should feel like a real game, not only a research sandbox.

## 6. Game Modes

### Mode: Observer Simulation

The user watches AI agents live in the world.

Requirements:

- Start, pause, resume, and speed up simulation.
- Inspect any agent.
- Inspect any zone.
- View event timeline.
- View faction and economy dashboards.
- Export replay logs.

### Mode: Commander

The user gives high-level commands to one agent, party, guild, or faction.

Examples:

- "Form a party and clear the nearby cave."
- "Gather herbs for the settlement."
- "Defend Northwatch until morning."
- "Negotiate a trade route with the River Guild."

Requirements:

- Commands must be translated into agent goals.
- Agents may refuse impossible or dangerous commands.
- Agents should explain why they accepted, delayed, or rejected a command.

### Mode: Player Adventure

The user controls one player character while AI agents fill the world.

Requirements:

- Human can move, fight, loot, talk, trade, accept quests, and join parties.
- AI party members can follow, assist, heal, tank, or retreat.
- Human player actions create world events and affect reputation.

## 7. World Structure

### Entity: World

Fields:

- id
- name
- seed
- current_time
- day_length_minutes
- simulation_speed
- ruleset_version
- created_at
- updated_at

Rules:

- World generation must be deterministic from seed.
- World state must be saveable and reloadable.

### Entity: Zone

Fields:

- id
- world_id
- name
- biome
- danger_level
- recommended_level_min
- recommended_level_max
- controlling_faction_id
- weather_state
- discovered_by_agent_ids
- created_at
- updated_at

Biomes:

- forest
- swamp
- desert
- mountain
- tundra
- city
- dungeon
- cave
- coast
- ruins

Rules:

- Higher danger zones spawn stronger monsters and better loot.
- Weather affects visibility, movement speed, and some abilities.

### Entity: Location

Fields:

- id
- zone_id
- name
- type
- x
- y
- z
- is_safe_area
- controlling_faction_id
- created_at
- updated_at

Types:

- town
- camp
- dungeon_entrance
- resource_node
- road
- shrine
- market
- forge
- inn
- boss_lair
- graveyard

## 8. Agent System

### Entity: Agent

Fields:

- id
- name
- species
- class
- level
- experience
- health
- mana
- stamina
- current_zone_id
- current_location_id
- faction_id
- guild_id
- party_id
- role
- personality_json
- goals_json
- memory_summary
- status
- created_at
- updated_at
- deleted_at

Species:

- human
- elf
- dwarf
- orc
- goblin
- undead
- drakekin
- construct

Classes:

- warrior
- mage
- priest
- rogue
- ranger
- paladin
- shaman
- warlock
- druid
- crafter
- merchant
- scout

Roles:

- tank
- healer
- damage
- support
- gatherer
- crafter
- trader
- commander
- scout

Statuses:

- idle
- traveling
- fighting
- gathering
- crafting
- trading
- resting
- dead
- incapacitated
- in_party
- in_raid

Rules:

- Agents must have persistent memory.
- Agents must have short-term tactical state and long-term strategic goals.
- Agents must choose actions based on class, role, personality, inventory, health, relationships, and world events.
- Dead agents respawn at a graveyard after a configurable delay unless permadeath mode is enabled.

## 9. Agent Intelligence

### Decision Layers

The AI agent must use layered decision-making:

1. Reflex layer

Fast deterministic decisions for urgent events.

Examples:

- Heal self if health is critical.
- Flee if outmatched.
- Attack hostile enemy in range.
- Stop moving if path is blocked.

2. Tactical layer

Short-horizon planning for combat, gathering, travel, and party coordination.

Examples:

- Choose target.
- Use interrupt.
- Move out of danger area.
- Protect healer.
- Consume potion.

3. Strategic layer

Long-horizon planning for quests, reputation, crafting, leveling, guild goals, and settlement needs.

Examples:

- Choose quest chain.
- Save gold for mount.
- Join a guild.
- Farm materials for armor.
- Recruit party for dungeon.

4. Narrative layer

Optional LLM-backed or rule-backed reasoning that explains choices in natural language.

Requirements:

- The simulation must not require an LLM call every tick.
- Reflex and tactical decisions must run deterministically.
- Strategic decisions may run periodically.
- Narrative explanations may be generated lazily on inspection.

### Entity: AgentMemory

Fields:

- id
- agent_id
- memory_type
- subject_type
- subject_id
- content
- importance
- sentiment
- created_at
- last_accessed_at
- expires_at

Memory types:

- event
- relationship
- location
- combat
- trade
- quest
- failure
- achievement
- rumor

Rules:

- Important memories persist longer.
- Repeated events should compress into summaries.
- Agents can forget low-importance memories.
- Memory retrieval must support search by subject and importance.

### Entity: AgentGoal

Fields:

- id
- agent_id
- title
- description
- priority
- status
- goal_type
- target_entity_type
- target_entity_id
- deadline_world_time
- created_at
- updated_at

Goal types:

- survive
- level_up
- complete_quest
- gather_resource
- craft_item
- earn_gold
- help_ally
- defeat_enemy
- explore_zone
- join_party
- defend_location
- trade
- rest

Rules:

- Each agent must always have at least one survival goal.
- Conflicting goals must be ranked by priority.
- Failed goals should produce memory and possibly a new recovery goal.

## 10. Combat System

### Entity: Ability

Fields:

- id
- name
- class_requirement
- level_requirement
- resource_type
- resource_cost
- cooldown_seconds
- range
- cast_time_seconds
- effect_type
- effect_value
- threat_value
- area_radius
- created_at
- updated_at

Effect types:

- damage
- heal
- shield
- buff
- debuff
- taunt
- interrupt
- summon
- movement

Rules:

- Abilities must respect cooldowns.
- Casts can be interrupted.
- Area abilities affect multiple targets.
- Threat generation influences monster target choice.

### Entity: CombatEncounter

Fields:

- id
- zone_id
- location_id
- status
- started_at
- ended_at
- participant_agent_ids
- participant_monster_ids
- event_log_json

Statuses:

- active
- won_by_agents
- won_by_monsters
- escaped
- reset

Combat requirements:

- Turn-based or tick-based combat is acceptable.
- Agents must select abilities based on role.
- Tanks should attempt to hold threat.
- Healers should prioritize low-health allies.
- Damage dealers should focus priority targets.
- Agents should retreat if defeat is likely.

## 11. Monster and Boss System

### Entity: Monster

Fields:

- id
- name
- species
- level
- health
- abilities
- loot_table_id
- faction_id
- zone_id
- location_id
- behavior_type
- respawn_seconds
- status

Behavior types:

- passive
- aggressive
- defensive
- patrolling
- boss
- elite

### Entity: Boss

Fields:

- id
- monster_id
- phase_count
- enrage_timer_seconds
- mechanics_json
- required_party_size
- recommended_level

Requirements:

- Bosses must have multi-phase mechanics.
- Raids require coordination among agents.
- Boss fights must produce detailed event logs.
- Failed raids should create agent memories.

## 12. Party, Guild, and Social Systems

### Entity: Party

Fields:

- id
- leader_agent_id
- name
- status
- goal_id
- member_agent_ids
- loot_rule
- created_at
- updated_at

Loot rules:

- leader_assigns
- round_robin
- need_before_greed
- free_for_all

Rules:

- Party size defaults to 5.
- Party should prefer tank, healer, and damage role balance.
- Party leader can set shared goal.
- Agents may leave party if goal conflicts strongly with personal goals.

### Entity: Guild

Fields:

- id
- name
- faction_id
- leader_agent_id
- member_agent_ids
- reputation
- bank_gold
- bank_items_json
- charter_json
- created_at
- updated_at

Guild requirements:

- Guilds can recruit agents.
- Guilds can create group goals.
- Guilds can schedule raids.
- Guilds can control settlements if reputation is high enough.

### Entity: Relationship

Fields:

- id
- source_agent_id
- target_agent_id
- trust
- fear
- respect
- rivalry
- friendship
- last_interaction_at

Rules:

- Helping another agent increases trust.
- Abandoning party during danger decreases trust.
- Competing for loot may increase rivalry.
- Healing or saving an agent increases respect.

## 13. Quest System

### Entity: Quest

Fields:

- id
- title
- description
- giver_entity_type
- giver_entity_id
- required_level
- faction_id
- prerequisites_json
- objectives_json
- rewards_json
- repeatable
- status

Objective types:

- kill
- collect
- escort
- explore
- craft
- deliver
- defend
- negotiate
- dungeon_clear
- boss_kill

### Entity: QuestProgress

Fields:

- id
- quest_id
- agent_id
- status
- objective_progress_json
- accepted_at
- completed_at

Rules:

- Agents should choose quests appropriate to level, location, faction, and goals.
- Quest completion grants experience, gold, items, and reputation.
- Failed escort or defend quests may alter world state.

## 14. Economy, Crafting, and Inventory

### Entity: Item

Fields:

- id
- name
- item_type
- rarity
- level_requirement
- stats_json
- stackable
- max_stack
- vendor_value

Item types:

- weapon
- armor
- consumable
- crafting_material
- quest_item
- mount
- tool
- currency

Rarities:

- common
- uncommon
- rare
- epic
- legendary

### Entity: Inventory

Fields:

- id
- owner_type
- owner_id
- item_id
- quantity
- durability
- bound
- created_at
- updated_at

Rules:

- Quantity cannot be negative.
- Bound items cannot be traded.
- Durability decreases through combat.

### Entity: MarketListing

Fields:

- id
- seller_agent_id
- item_id
- quantity
- unit_price
- status
- listed_at
- sold_at

Rules:

- Agents can list, buy, and cancel listings.
- Prices should react to scarcity and demand.
- Large-agent mode must support market aggregation for performance.

### Entity: CraftingRecipe

Fields:

- id
- name
- profession
- required_skill
- input_items_json
- output_item_id
- output_quantity
- crafting_time_seconds

Rules:

- Crafting consumes input items.
- Crafting may fail if skill is too low.
- Repeated crafting improves profession skill.

## 15. Factions and Reputation

### Entity: Faction

Fields:

- id
- name
- alignment
- controlled_zone_ids
- allied_faction_ids
- enemy_faction_ids
- reputation_rules_json

Rules:

- Agents gain or lose reputation with factions.
- Enemy factions attack or refuse trade.
- Faction control can change through war, quests, or settlement defense.

### Entity: Reputation

Fields:

- id
- agent_id
- faction_id
- score
- rank
- updated_at

Ranks:

- hated
- hostile
- neutral
- friendly
- honored
- revered
- exalted

## 16. Simulation Engine Requirements

### Tick System

Requirements:

- The simulation must support fixed ticks.
- Tick rate must be configurable.
- Agent decisions do not all need to run every tick.
- Expensive decisions must be scheduled.
- The engine must support pause, resume, speed multiplier, and deterministic replay.

### Event Bus

Every major state change must create an event.

Event examples:

- agent_created
- agent_goal_created
- agent_goal_failed
- agent_traveled
- combat_started
- combat_ended
- quest_accepted
- quest_completed
- item_looted
- item_traded
- party_formed
- guild_created
- boss_defeated
- market_listing_created
- faction_reputation_changed

### Entity: WorldEvent

Fields:

- id
- world_id
- tick
- timestamp
- event_type
- actor_type
- actor_id
- target_type
- target_id
- summary
- payload_json

Rules:

- Events are append-only.
- Events power logs, replay, dashboards, and agent memory extraction.

## 17. Scaling Requirements

### Small-Agent Mode

Requirements:

- Runs locally.
- Supports at least 50 agents.
- Supports visual UI.
- Stores state in SQLite or local JSON database.
- Uses deterministic rules for most decisions.

### Large-Agent Mode

Requirements:

- Supports simulation sharding by zone.
- Supports background workers.
- Supports batched agent decisions.
- Supports aggregated market and event views.
- Supports event-sourced replay.
- Supports snapshotting world state.

Architecture expectations:

- Separate simulation engine from UI.
- Separate agent decision service from world state storage.
- Use message/event queues or an abstraction that can later map to queues.
- Avoid global locks where possible.
- Design for zone-level partitioning.

## 18. UI Requirements

The first screen must be the live world dashboard.

Required views:

- World Dashboard
- Zone Map
- Agent Inspector
- Party View
- Guild View
- Combat Replay
- Quest Board
- Market
- Inventory
- Faction Reputation
- Event Timeline
- Simulation Settings

### World Dashboard

Must show:

- Active agents
- Active parties
- Active combats
- Recent deaths
- Quests completed today
- Boss attempts
- Market volume
- Faction conflicts
- Simulation speed
- Current world time

### Agent Inspector

Must show:

- Current location
- Current goal
- Health and resources
- Inventory
- Equipment
- Memories
- Relationships
- Quest progress
- Recent events
- Decision explanation

### Zone Map

Must show:

- Locations
- Agents
- Monsters
- Resource nodes
- Party movement
- Danger level
- Faction control

### Combat Replay

Must show:

- Participants
- Timeline of ability usage
- Damage and healing
- Threat changes
- Deaths
- Retreats
- Final outcome

## 19. CLI Requirements

The CLI must support human-readable output and JSON output.

Required commands:

```bash
eidolon init
eidolon seed --agents 50
eidolon run --ticks 1000
eidolon status
eidolon pause
eidolon resume
eidolon agent list
eidolon agent show <agent-id>
eidolon agent goal add <agent-id> --type complete_quest --target <quest-id>
eidolon party list
eidolon party form --leader <agent-id> --goal <goal-id>
eidolon quest list
eidolon combat list
eidolon event tail --limit 50
eidolon market list
eidolon export --out world.json
eidolon import world.json
```

CLI acceptance criteria:

- Commands return non-zero exit codes on expected errors.
- `--json` output is valid JSON.
- Missing IDs produce friendly errors.
- Simulation commands must not corrupt state if interrupted.

## 20. Persistence Requirements

Required:

- Save and load world state.
- Store append-only world events.
- Store agent memory.
- Store agent goals.
- Store inventories and market listings.
- Support migration versioning.
- Support deterministic seed data.
- Support export and import.

Recommended:

- SQLite for local prototype.
- Event log table for replay.
- Snapshot table for fast reload.

## 21. Testing Requirements

### Unit Tests

Must cover:

- Combat damage and healing calculations.
- Threat target selection.
- Agent goal priority selection.
- Quest objective progress.
- Inventory stack rules.
- Market listing purchase flow.
- Relationship updates.
- Reputation rank changes.
- Tick scheduler.
- Deterministic world generation.

### Integration Tests

Must cover:

- Agent accepts quest, travels, kills monster, loots item, completes quest.
- Party forms with tank, healer, and damage roles.
- Party enters dungeon and completes combat.
- Agent buys item from market.
- Agent crafts item from gathered materials.
- Guild schedules raid.
- Boss fight produces replay events.
- Export and import preserve world state.

### End-to-End Tests

Must cover:

- Dashboard loads seeded world.
- Agent inspector shows live state.
- Simulation can run 100 ticks from UI.
- Combat replay opens after a fight.
- Market listing can be created and purchased.
- Event timeline updates.

## 22. Seed World Requirements

Seed command must create:

- 1 world
- 8 zones
- 30 locations
- 50 agents by default
- 5 factions
- 10 guilds
- 20 parties
- 100 monsters
- 6 bosses
- 80 quests
- 200 items
- 40 crafting recipes
- 100 market listings
- 1,000 initial world events

Seed must be deterministic by seed value.

## 23. Performance Requirements

Small-agent mode:

- 50 agents at 10 simulation ticks per second.
- Dashboard updates at least once per second.
- Agent inspector opens within 300 ms.
- 100 tick simulation completes in under 10 seconds.

Large-agent mode:

- 10,000 agents with batched decisions.
- 100,000 world events searchable within 2 seconds.
- Zone-level dashboard loads within 1 second.
- Snapshot save completes within 30 seconds.

## 24. Safety and Control Requirements

The user must be able to:

- Pause simulation immediately.
- Stop all agent decisions.
- Disable LLM-backed reasoning.
- Run deterministic rule-only mode.
- Reset world from seed.
- Export logs for debugging.

Agents must not:

- Execute shell commands.
- Access local files outside game state.
- Call external services unless explicitly configured.
- Modify game rules at runtime unless permission is granted.

## 25. Milestones

### Milestone 1: Local Prototype Scaffold

Deliver:

- App scaffold.
- CLI scaffold.
- SQLite schema.
- Seed command.
- World dashboard placeholder.
- Basic tests.

### Milestone 2: Simulation Core

Deliver:

- Tick engine.
- World events.
- Agent state.
- Movement.
- Goals.
- Save/load.

### Milestone 3: Combat and Quests

Deliver:

- Abilities.
- Monsters.
- Combat encounters.
- Quest progress.
- Loot.
- Combat logs.

### Milestone 4: Social and Economy

Deliver:

- Parties.
- Guilds.
- Relationships.
- Inventory.
- Crafting.
- Market.

### Milestone 5: Agent Intelligence

Deliver:

- Reflex layer.
- Tactical layer.
- Strategic goal planner.
- Memory retrieval.
- Decision explanations.

### Milestone 6: UI and Replay

Deliver:

- Live dashboard.
- Zone map.
- Agent inspector.
- Combat replay.
- Event timeline.
- Market UI.

### Milestone 7: Scale and Hardening

Deliver:

- Batched decisions.
- Zone sharding abstraction.
- Snapshots.
- Import/export.
- Performance tests.
- Documentation.

## 26. Definition of Done

The project is complete when:

- A user can initialize a world.
- A user can seed agents, zones, monsters, quests, and items.
- The simulation runs autonomously.
- Agents can form parties.
- Agents can complete quests.
- Agents can fight monsters.
- Agents can loot and trade items.
- Agents can remember important events.
- The UI shows live world state.
- The CLI can inspect and control simulation.
- World events can be replayed.
- Tests verify core systems.
- Documentation explains setup, commands, architecture, and limitations.

## 27. Hard Mode Evaluation

Weak implementation:

- Static fantasy page only.
- No autonomous agents.
- No persistent world state.
- No simulation ticks.
- No combat system.
- No event log.
- No CLI.
- No tests.

Strong implementation:

- Deterministic simulation.
- Real agent goals and memory.
- Inspectable decision logs.
- Working combat and quests.
- Party coordination.
- Persistent SQLite state.
- Replayable event timeline.
- Clear scaling architecture.
- Good seed world.
- Meaningful tests.

