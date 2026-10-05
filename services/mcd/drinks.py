"""
ドリンクの種類分け

⚠️ カタログは全部まとめて「ドリンク」としか書いていない。
   セットのドリンク枠には24種類ほど入るため、1つの長い一覧に
   並べると選びにくい（Discordの選択肢は一度に5〜6件しか見えない）。
   名前から種類を見分けて、同じ仲間が隣り合うようにする。

⚠️ **見分けに失敗しても害が無いようにすること。**
   分からないものは「その他」に入れて最後に並べるだけで、
   選べなくなるわけではない。
"""

from __future__ import annotations

# (並び順, 絵文字, 呼び名, 名前に含まれる語)
#
# ⚠️ 上から順に当てはめる。先に書いたものが勝つ。
#    「アイスカフェラテ」は お茶(ティー) ではなく コーヒー なので、
#    コーヒーを先に置いてある。
_GROUPS: tuple[tuple[int, str, str, tuple[str, ...]], ...] = (
    (0, "🥤", "炭酸", ("コカ・コーラ", "コカコーラ", "スプライト", "ファンタ",
                      "ジンジャーエール")),
    (1, "🧃", "ジュース", ("ミニッツメイド", "Qoo", "クー", "野菜生活",
                        "オレンジ", "アップル", "ぶどう")),
    (2, "☕", "コーヒー", ("コーヒー", "カフェラテ", "キャラメルラテ",
                        "エスプレッソ", "カプチーノ", "マキアート")),
    (3, "🍵", "お茶", ("ティー", "烏龍茶", "ウーロン", "爽健美茶", "緑茶", "紅茶")),
    (4, "🥛", "乳製品", ("ミルク", "シェイク", "ミルクセーキ")),
)

OTHER = (9, "🍹", "その他")


def group_of(name: str) -> tuple[int, str, str]:
    """
    その飲み物の (並び順, 絵文字, 呼び名)。分からなければ「その他」。
    """
    text = str(name or "")
    for order, emoji, label, words in _GROUPS:
        if any(w in text for w in words):
            return order, emoji, label
    return OTHER


def emoji_of(name: str) -> str:
    """名前に付ける絵文字。"""
    return group_of(name)[1]


def is_drink_slot(candidates) -> bool:
    """
    その選択枠がドリンクの枠か。

    ⚠️ 枠コードで判断しない。枠コードは店舗やセットごとに違い、
       新しいものが出るたびに表を直すことになる。
       **中身を見て**決めれば、その手間が要らない。
       半分以上が種類を見分けられたらドリンクの枠とみなす。
    """
    names = [getattr(c, "name", "") for c in candidates or []]
    if len(names) < 4:
        return False
    known = sum(1 for n in names if group_of(n) is not OTHER
                and group_of(n)[0] != OTHER[0])
    return known * 2 >= len(names)
