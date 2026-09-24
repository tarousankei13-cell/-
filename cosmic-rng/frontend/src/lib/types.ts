export type Tier = 1 | 2 | 3 | 4 | 5 | 6 | 7 | 8;
export type RarityKey = "common" | "rare" | "epic" | "legendary" | "secret" | "ultra_secret" | "mythic" | "admin";

export interface Visual {
  shape?: string;
  colors?: string[];
  glow?: string;
  fx?: string;
  theme?: string;
  tier?: number;
  aura?: string;
  roll_effect?: string;
  ui_theme?: string;
  background?: string;
  particles?: string;
  silhouette?: boolean;
  procedural?: boolean;
}

export interface ItemInfo {
  id: number;
  key: string;
  name: string;
  name_ja?: string | null;
  description: string;
  lore?: string;
  kind: string;
  rarity: RarityKey;
  tier: number;
  odds: number | null;
  display_odds: string | null;
  sell_value: number;
  visual: Visual;
  animation?: string | null;
  sound?: string | null;
  tradeable: boolean;
  biomes: string[];
  serial?: number | null;
}

export interface RarityDef {
  key: RarityKey;
  name: string;
  name_ja?: string | null;
  tier: number;
  min_odds: number;
  color: string;
  color2: string;
  cutscene: string;
}

export interface BiomeTheme {
  bg?: string[];
  nebula?: string[];
  accent?: string;
  accent2?: string;
  particles?: string;
  particle_color?: string;
  intensity?: number;
  bgm?: string;
  fx?: string;
  vignette?: number;
}

export interface BiomeState {
  key: string;
  name: string;
  name_ja?: string | null;
  kind: string;
  description: string;
  luck_mult: number;
  theme: BiomeTheme;
  started_at: string;
  ends_at: string | null;
  forced: boolean;
  locked_until: string | null;
  state: { key: string; name: string; luck_mult: number; description: string; ends_at: string | null } | null;
  reveal?: { next_change_at: string; next_biome: string | null; next_state_at: string | null; next_state: string | null };
}

export interface LuckBreakdown {
  base: number;
  equipment: number;
  biome: number;
  temporary: number;
  special: number;
  event: number;
  other: number;
  final: number;
}

export interface ActiveEffect {
  id: number;
  name: string;
  name_ja?: string | null;
  source_type: string;
  source_key: string;
  effect_type: string;
  value: number;
  stack_mode: string;
  remaining_rolls: number | null;
  expires_at: string | null;
  biome_keys: string[];
  params: Record<string, unknown>;
}

export interface EquipVisual {
  slot: string;
  key: string;
  name: string;
  name_ja?: string | null;
  rarity: RarityKey;
  visual: Visual;
  quality_tier?: string;
  instance_id?: number;
  aura?: string;
}

export interface HudState {
  stardust: number;
  level: number;
  xp: number;
  xp_level: number;
  xp_next: number;
  roll_counter: number;
  next_roll_at: string | null;
  cooldown: number;
  server_time: string;
  luck: LuckBreakdown;
  next_special: boolean;
  special_in: number;
  special_interval: number;
  roll_speed: number;
  effects: ActiveEffect[];
  equipment: EquipVisual[];
  biome: BiomeState;
  auto_roll: boolean;
  unlocked: string[];
  unlocks: string[];
  reveal_rng: boolean;
  reveal_secrets: boolean;
}

export interface Fortune {
  key: string;
  label: string;
  label_ja?: string | null;
  top_percent: number;
  score: number;
}

export interface AchievementGrant {
  key: string;
  name: string;
  name_ja?: string | null;
  description: string;
  tier: string;
  category: string;
  world_first: boolean;
  rewards: { stardust?: number; items?: { key: string; name: string; qty: number }[]; cosmetics?: string[] };
}

export interface ProgressResult {
  quests_completed: { id: number; key: string; name: string; kind: string }[];
  achievements: AchievementGrant[];
  level_up: { from: number; to: number } | null;
  unlocked: string[];
}

export interface FirstDiscovery {
  item: ItemInfo;
  odds: number;
  discovered_at: string;
  player: string;
  player_id: number;
  offline?: boolean;
}

export interface RollResult {
  id: number;
  number: number;
  item: ItemInfo;
  instance_ids: number[];
  odds: number;
  final_chance: number;
  final_odds: number | null;
  luck: LuckBreakdown;
  biome: { key: string; name: string; name_ja?: string | null; state: string | null };
  special: boolean;
  hidden_special: { key: string; name: string } | null;
  effects_applied: string[];
  fortune: Fortune;
  auto_deleted: boolean;
  auto_delete_mode: string | null;
  auto_sold: number;
  overflow: boolean;
  first_discovery: FirstDiscovery | null;
  new_collection: boolean;
  duplicated: number;
  forced: boolean;
  best_of: number;
  preview_used: boolean;
  xp: number;
  cosmic_eye: { top: { name: string; rarity: RarityKey; p: number }[]; table_size: number } | null;
  progress: ProgressResult;
}

export interface OfflineSummary {
  kind: string;
  batch_id: number;
  rolls: number;
  window_start: string;
  window_end: string;
  seconds: number;
  totals: { kept: number; deleted: number; sold: number; overflow: number; stardust: number };
  specials: number;
  luck_max: number;
  results: { item: ItemInfo; count: number; kept: number; sold: number; deleted: number; first: boolean; reveal: boolean }[];
  distinct: number;
  first_discoveries: FirstDiscovery[];
  biomes: { key: string; name: string }[];
  efficiency?: number;
}

export interface RollResponse {
  roll: RollResult;
  offline: OfflineSummary | null;
  state: HudState;
}

export interface UserBrief {
  id: number;
  name: string;
  name_ja?: string | null;
  username: string;
  avatar: string | null;
  level: number;
  title: string | null;
}

export interface FeedEvent {
  id: number;
  type: string;
  payload: Record<string, any>;
  user: UserBrief | null;
  anonymous: boolean;
  created_at: string;
}

export interface Notification {
  id: number;
  type: string;
  title: string;
  body: string;
  data: Record<string, unknown>;
  read: boolean;
  created_at: string;
}

export interface PlayerSettings {
  audio: { master: number; bgm: number; sfx: number; muted: boolean };
  graphics: { quality: "high" | "medium" | "low" | "minimal"; particles: number; reduced_motion: boolean; screen_shake: boolean; background_fx: boolean };
  roll: { speed: "normal" | "fast" | "ultra"; cutscenes: boolean; full_cutscene_min_tier: RarityKey; skip_confirm_min_tier: RarityKey };
  auto_skip: { enabled: boolean; threshold: number };
  auto_delete: { enabled: boolean; max_odds: number; tiers: RarityKey[]; mode: "delete" | "sell"; protect_new: boolean; show_deleted: boolean };
  notifications: { world_feed: boolean; feed_min_tier: RarityKey; toasts: boolean; trades: boolean; gifts: boolean };
  privacy: { public_profile: boolean; public_drops: boolean; show_inventory: boolean };
  ui: { font_scale: number; high_contrast: boolean };
}

export interface Me {
  user: UserBrief & {
    discord_id: string | null;
    email: string | null;
    status: string;
    status_reason: string | null;
    stardust: number;
    xp: number;
    roll_counter: number;
    auto_roll: boolean;
    title_key: string | null;
    background: string | null;
    created_at: string;
  };
  csrf: string;
  is_admin: boolean;
  is_super_admin: boolean;
  admin_mode: boolean;
  settings: PlayerSettings;
  unread: number;
  unlocked: string[];
}

export interface PublicConfig {
  rarities: RarityDef[];
  biomes: { key: string; name: string; kind: string; theme: BiomeTheme; hidden: boolean; states: { key: string; name: string; theme: BiomeTheme }[] }[];
  unlocks: Record<string, number>;
  special_interval: number;
  features: Record<string, boolean>;
  maintenance: { enabled: boolean; message: string };
  discord_login: boolean;
  registration_open: boolean;
  rng_version: number;
  content_version: number;
}

export interface InventoryGroup {
  item: ItemInfo;
  count: number;
  last_obtained: string | null;
  locked: number;
  favorite_instances: number;
  listed: number;
  favorite: boolean;
}

export interface Instance {
  id: number;
  item_id: number;
  serial: number | null;
  source: string;
  state: string;
  locked: boolean;
  favorite: boolean;
  meta: Record<string, any>;
  obtained_at: string | null;
  item?: ItemInfo;
}

export interface Cinematic {
  artifact: string;
  name: string;
  name_ja?: string | null;
  theme: string;
  tier: number;
  visual: Visual;
  user?: UserBrief;
  target?: string;
}
