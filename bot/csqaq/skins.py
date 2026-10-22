# -*- coding: utf-8 -*-
"""CS2 饰品中英文名映射表 + 候选英文名生成。

用途:用户输入中文关键词(如「血腥运动」),本地快照查不到时,
用本表生成可能的英文 market_hash_name 候选,调 CSQAQ getPriceByMarketHashName 实时查价。
覆盖常见武器/皮肤/磨损,无需覆盖全量(查不到时如实告知即可)。
"""

# 武器 中文 -> 英文(常见主力)
WEAPONS_ZH_EN = {
    "AK-47": "AK-47",
    "M4A1": "M4A1-S",
    "M4A1-S": "M4A1-S",
    "M4A4": "M4A4",
    "AWP": "AWP",
    "USP": "USP-S",
    "USP-S": "USP-S",
    "沙漠之鹰": "Desert Eagle",
    "沙鹰": "Desert Eagle",
    "格洛克": "Glock-18",
    "P250": "P250",
    "P90": "P90",
    "MAC-10": "MAC-10",
    "MP9": "MP9",
    "MP7": "MP7",
    "UMP": "UMP-45",
    "SSG 08": "SSG 08",
    "鸟狙": "SSG 08",
    "AUG": "AUG",
    "SG 553": "SG 553",
    "法玛斯": "FAMAS",
    "加利尔": "Galil AR",
    "加利尔AR": "Galil AR",
    "内格夫": "Negev",
    "M249": "M249",
    "FN57": "FN57",
    "TEC-9": "Tec-9",
    "CZ75": "CZ75-Auto",
    "R8": "R8 Revolver",
    "双持贝瑞塔": "Dual Berettas",
    "蝴蝶刀": "★ Butterfly Knife",
    "爪子刀": "★ Karambit",
    "折叠刀": "★ Flip Knife",
    "弯刀": "★ Falchion Knife",
    "猎杀者匕首": "★ Huntsman Knife",
    "骷髅匕首": "★ Skeleton Knife",
    "鲍伊猎刀": "★ Bowie Knife",
    "M9刺刀": "★ M9 Bayonet",
    "刺刀": "★ Bayonet",
    "穿肠刀": "★ Gut Knife",
    "短剑": "★ Stiletto Knife",
    "刺鳞刀": "★ Navaja Knife",
    "幽灵匕首": "★ Ghost Knife",
    "熊刀": "★ Ursus Knife",
    "折刀": "★ Classic Knife",
    "系绳匕首": "★ Cord Knife",
    "求生匕首": "★ Survival Knife",
    "爪刀": "★ Nomad Knife",
    "狂牙匕首": "★ Paracord Knife",
    "刀": "★ Karambit",
    "匕首": "★ Karambit",
    "专业手套": "★ Specialist Gloves",
    "运动手套": "★ Sport Gloves",
    "摩托手套": "★ Moto Gloves",
    "驾驶手套": "★ Driver Gloves",
    "血猎手套": "★ Bloodhound Gloves",
    "手部束带": "★ Hand Wraps",
    "九头蛇手套": "★ Hydra Gloves",
    "手套": "★ Sport Gloves",
}

# 皮肤 中文 -> 英文(常见皮肤,持续补充)
SKINS_ZH_EN = {
    "血腥运动": "Bloodsport",
    "渐变之色": "Fade",
    "渐变大理石": "Marble Fade",
    "多普勒": "Doppler",
    "伽玛多普勒": "Gamma Doppler",
    "表面淬火": "Case Hardened",
    "深红之网": "Crimson Web",
    "红线": "Redline",
    "二西莫夫": "Asiimov",
    "巨龙传说": "Dragon Lore",
    "咆哮": "Howl",
    "永恒之枪": "Gungnir",
    "野荷": "Wild Lotus",
    "潘多拉之盒": "Pandora's Box",
    "传承": "Legacy",
    "霓虹革命": "Neon Revolution",
    "水栽竹": "Hydroponic",
    "水栽竹2": "Hydroponic",
    "印花集": "Printstream",
    "死神": "Neo-Noir",
    "暴怒野兽": "Hyper Beast",
    "阿努比斯军团": "Legion of Anubis",
    "阿努比斯": "Legion of Anubis",
    "霓虹骑士": "Neon Rider",
    "火蛇": "Fire Serpent",
    "混乱点阵": "Chaos",
    "弹雨": "Contamination",
    "潜行者": "The Empress",
    "女皇": "The Empress",
    "皇后": "Queen Jaguar",
    "黄金藤蔓": "Gold Arabesque",
    "野火": "Wildfire",
    "闪电打击": "Lightning Strike",
    "蛇咬": "Snakebite",
    "蛇纹": "Snake Camo",
    "迷彩": "Camo",
    "黑水": "Blackwater",
    "离子": "Ion",
    "电流": "Electric Hive",
    "电路": "Circuit Board",
    "电路板": "Circuit Board",
    "赛博": "Cyber",
    "赛博朋克": "Cyberpunk",
    "像素": "Pixel",
    "像素化": "Pixel",
    "果冻": "Jelly",
    "冰霜": "Frost",
    "霜冻": "Frosty",
    "冰核": "Ice Core",
    "冰封": "Cold Blooded",
    "冷酷": "Cold Blooded",
    "熔岩": "Lava",
    "熔岩色": "Lava",
    "岩浆": "Magma",
    "火山": "Volcano",
    "太阳神": "Solar",
    "太阳": "Sunset",
    "日落": "Sunset",
    "黎明": "Dawn",
    "黄昏": "Twilight",
    "夜魔": "Nightmare",
    "噩梦": "Nightmare",
    "梦魇": "Nightmare",
    "邪恶": "Vice",
    "精英": "Elite Build",
    "精英之作": "Elite Build",
    "控制台": "Console",
    "轨道": "Orbit",
    "星轨": "Orbit Mk01",
    "轨道Mk01": "Orbit Mk01",
    "仙女座": "Andromeda",
    "仙女座2": "Andromeda",
    "火神": "Vulcan",
    "火卫": "Phobos",
    "波塞冬": "Poseidon",
    "波塞冬2": "Poseidon",
    "水神": "Poseidon",
    "极乐世界": "Elysium",
    "伊甸园": "Eden",
    "守护者": "Guardian",
    "哨兵": "Sentry",
    "十字军": "Crusader",
    "战斧": "Tomahawk",
    "猎户座": "Orion",
    "猎户2": "Orion",
    "猎户座2": "Orion",
    "天界": "Celestial",
    "天堂": "Paradise",
    "地狱": "Inferno",
    "炼狱": "Inferno",
    "殉道": "Martyr",
    "献祭": "Sacrifice",
    "祭品": "Sacrifice",
    "圣物": "Relic",
    "遗物": "Relic",
    "古董": "Antique",
    "古董2": "Antique",
    "锈蚀": "Corroded",
    "腐蚀": "Corroded",
    "锈蚀2": "Corroded",
    "科隆": "Cologne",
    "卡托维兹": "Katowice",
    "伦敦": "London",
    "安特卫普": "Antwerp",
    "巴黎": "Paris",
    "上海": "Shanghai",
    "哥本哈根": "Copenhagen",
    "印花": "Sticker",
    "贴纸": "Sticker",
    "全息": "Holo",
    "闪亮": "Foil",
    "金贴": "Foil",
    "胶囊": "Capsule",
    "收藏品": "Collection",
    "纪念品": "Souvenir",
    "纪念包": "Package",
    # 刀皮常用(高频补充)
    "自动化": "Autotronic",
    "自动装置": "Autotronic",
    "虎牙": "Tiger Tooth",
    "屠杀者": "Slaughter",
    "蓝钢": "Blue Steel",
    "大马士革钢": "Damascus Steel",
    "传说": "Lore",
    "夜色": "Night",
    "夜幕": "Night",
    "红宝石": "Ruby",
    "蓝宝石": "Sapphire",
    "黑珍珠": "Black Pearl",
    "翡翠": "Emerald",
    "绿宝石": "Emerald",
    "紫罗兰": "Ultraviolet",
    "紫外线": "Ultraviolet",
    "紫外": "Ultraviolet",
    "涂鸦": "Graffiti",
    "血网": "Crimson Web",
    "红网": "Crimson Web",
    "碳纤维": "Carbon Fiber",
    "碳素": "Carbon Fiber",
    "装甲": "Armor",
    "迷彩网": "Camo",
    "森林": "Forest",
    "丛林": "Jungle",
    "猎豹": "Leopard",
    "猎豹纹": "Leopard",
    "斑马": "Zebra",
    "蛇皮": "Snake",
    "蟒蛇": "Python",
    "蟒蛇纹": "Python",
    # 枪皮常用
    "瓦伦西亚": "Valencia",
    "勿忘我": "Forget Me Not",
    "绿宝石2": "Emerald",
    "碎裂": "Shatter",
    "破碎": "Shatter",
    "红鹰": "Red Eagle",
    "火烈鸟": "Flamingo",
    "咆哮2": "Howl",
    "咆哮3": "Howl",
    "遗产": "Legacy",
    "殖民地": "Colonial",
    "殖民地2": "Colonial",
    "殖民地3": "Colonial",
    "赛博2": "Cyber",
    "网络": "Network",
    "网络2": "Network",
    "侵蚀": "Corroded",
    "腐蚀2": "Corroded",
    "地牢": "Dungeon",
    "地牢2": "Dungeon",
    "黄金": "Gold",
    "金色": "Gold",
    "银": "Silver",
    "银色": "Silver",
    "青铜": "Bronze",
    "青铜2": "Bronze",
    "意式拉力": "Integrale",
    "拉力": "Integrale",
    "狮子之日": "Sun in Leo",
    "狮子座": "Sun in Leo",
    "血虎": "Blood Tiger",
}

# 磨损 中文 -> 英文
EXTERIORS_ZH_EN = {
    "崭新出厂": "Factory New",
    "略有磨损": "Minimal Wear",
    "久经沙场": "Field-Tested",
    "破损不堪": "Well-Worn",
    "战痕累累": "Battle-Scarred",
}

# 武器箱/胶囊 中文 -> 英文(直接整名查询,不走 武器|皮肤 组合)
CASES_ZH_EN = {
    "光谱武器箱": "Spectrum Case",
    "光谱": "Spectrum Case",
    "反冲武器箱": "Recoil Case",
    "反冲": "Recoil Case",
    "棱彩2号": "Chroma 2 Case",
    "棱彩3号": "Chroma 3 Case",
    "棱彩": "Chroma Case",
    "梦魇武器箱": "Nightmare Case",
    "蛇噬武器箱": "Snakebite Case",
    "裂空武器箱": "Fracture Case",
    "暗影武器箱": "Shadow Case",
    "狂牙武器箱": "Operation Riptide Case",
    "反恐武器箱": "Operation Broken Fang Case",
    "武器箱": "Case",
    "胶囊": "Capsule",
}

# 命中英文名里要剔除的通用词(避免把武器名误当皮肤名)
_SKIP_TOKENS = ("AK-47", "M4A1", "AWP", "USP", "沙漠之鹰", "蝴蝶刀", "爪子刀", "折叠刀", "崭新", "略有", "久经", "破损", "战痕")


def zh_to_en_candidates(text: str, max_per_weapon: int = 5) -> list:
    """把用户中文关键词转成候选英文 market_hash_name 列表。

    规则:
    - 拆出武器名(如 AK-47)、皮肤名(如 血腥运动)、磨损(如 崭新出厂)
    - 组合成 "武器 | 皮肤 (磨损)" 候选;只给皮肤名时用常见武器子集
    """
    if not text:
        return []
    text = text.strip()

    # 0) 武器箱/胶囊:整名直接查询
    if "箱" in text or "胶囊" in text:
        for zh, en in sorted(CASES_ZH_EN.items(), key=lambda x: -len(x[0])):
            if zh in text:
                return [en]
    candidates = []

    # 1) 提取武器
    weapon_en = None
    for zh, en in WEAPONS_ZH_EN.items():
        if zh in text:
            weapon_en = en
            break

    # 2) 提取磨损
    ext_en = None
    for zh, en in EXTERIORS_ZH_EN.items():
        if zh in text:
            ext_en = en
            break

    # 3) 提取皮肤(先匹配最长的中文皮肤名;跳过磨损词,避免"崭新"被当皮肤)
    skin_en = None
    for zh, en in sorted(SKINS_ZH_EN.items(), key=lambda x: -len(x[0])):
        if zh in EXTERIORS_ZH_EN:
            continue
        if zh in text:
            skin_en = en
            break

    # 4) 生成候选
    if weapon_en and skin_en:
        if ext_en:
            candidates.append(f"{weapon_en} | {skin_en} ({ext_en})")
        else:
            for e in EXTERIORS_ZH_EN.values():
                candidates.append(f"{weapon_en} | {skin_en} ({e})")
    elif skin_en:
        # 只有皮肤名:常见武器子集
        common = ["AK-47", "M4A1-S", "M4A4", "AWP", "USP-S", "Desert Eagle",
                  "Glock-18", "P250", "MAC-10", "MP9", "SSG 08", "AUG",
                  "FAMAS", "Galil AR", "★ Karambit", "★ Butterfly Knife",
                  "★ Flip Knife", "★ M9 Bayonet", "★ Skeleton Knife"]
        for w in common:
            if ext_en:
                candidates.append(f"{w} | {skin_en} ({ext_en})")
            else:
                for e in EXTERIORS_ZH_EN.values():
                    candidates.append(f"{w} | {skin_en} ({e})")
    elif weapon_en:
        # 只有武器名:列出该武器常见组合由调用方兜底(这里不生成,避免爆炸)
        return [weapon_en]

    return candidates
