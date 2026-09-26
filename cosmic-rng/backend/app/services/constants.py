"""Shared gameplay constants that are structural (not balance) and therefore not admin-tunable."""

# Items at or above this tier get a global serial number ("#12 in the world").
# Serial assignment locks the item row briefly, so it is skipped for very common tiers.
SERIAL_MIN_TIER = 3

# Tier numbers
TIER_COMMON, TIER_RARE, TIER_EPIC, TIER_LEGENDARY, TIER_SECRET, TIER_ULTRA, TIER_MYTHIC, TIER_ADMIN = range(1, 9)

TIER_KEYS = {1: "common", 2: "rare", 3: "epic", 4: "legendary", 5: "secret", 6: "ultra_secret", 7: "mythic", 8: "admin"}
TIER_BY_KEY = {v: k for k, v in TIER_KEYS.items()}

# Roll flags (bitmask stored in rolls.flags)
FLAG_SPECIAL = 1
FLAG_AUTO = 2
FLAG_OFFLINE = 4
FLAG_AUTO_DELETED = 8
FLAG_FIRST_DISCOVERY = 16
FLAG_FORCED = 32
FLAG_ARTIFACT = 64
FLAG_AUTO_SOLD = 128
FLAG_OVERFLOW = 256
FLAG_HIDDEN_SPECIAL = 512
FLAG_BURST = 1024
FLAG_PREVIEW = 2048
