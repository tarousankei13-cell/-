import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    TOKEN: str = os.getenv("BOT_TOKEN", "")
    PREFIX: str = os.getenv("BOT_PREFIX", "!")
    GUILD_ID: int | None = int(gid) if (gid := os.getenv("GUILD_ID")) else None

    ADMIN_ROLE: str = os.getenv("ADMIN_ROLE", "Admin")
    STAFF_ROLE: str = os.getenv("STAFF_ROLE", "Staff")

    ORDER_LOG_CHANNEL: int | None = int(ch) if (ch := os.getenv("ORDER_LOG_CHANNEL")) else None
    TICKET_CATEGORY: int | None = int(ch) if (ch := os.getenv("TICKET_CATEGORY")) else None
    ANNOUNCE_CHANNEL: int | None = int(ch) if (ch := os.getenv("ANNOUNCE_CHANNEL")) else None

    DAILY_REWARD: int = int(os.getenv("DAILY_REWARD", 500))
    STARTING_BALANCE: int = int(os.getenv("STARTING_BALANCE", 1000))
    CURRENCY_NAME: str = os.getenv("CURRENCY_NAME", "ポイント")
    CURRENCY_EMOJI: str = os.getenv("CURRENCY_EMOJI", "💰")

    MAX_CART_ITEMS: int = int(os.getenv("MAX_CART_ITEMS", 20))
    MAX_CART_QUANTITY: int = int(os.getenv("MAX_CART_QUANTITY", 99))

    DB_PATH: str = "shop.db"

    # Embed color palette
    COLOR_PRIMARY: int = 0x5865F2
    COLOR_SUCCESS: int = 0x57F287
    COLOR_WARNING: int = 0xFEE75C
    COLOR_ERROR: int = 0xED4245
    COLOR_INFO: int = 0x00B0F4
    COLOR_SHOP: int = 0xFF6B6B
