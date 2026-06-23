-- 每次采集的原始快照，每轮一个 run_id
-- scope_key 格式：card_order_<industry_name>_<category_name>，如 card_order_智能家居_五金
CREATE TABLE IF NOT EXISTS products_snapshot (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id           TEXT    NOT NULL,          -- 本轮批次标识 (ISO datetime)
    scope_key        TEXT    NOT NULL,          -- 榜单维度+类目，如 "card_order_智能家居_五金"
    rank             INTEGER NOT NULL,
    product_id       TEXT    NOT NULL,
    product_title    TEXT,
    product_url      TEXT,
    image            TEXT    DEFAULT '',         -- 商品主图 URL（接口 product_info.image_url）
    price_range      TEXT,
    pay_amount       TEXT,
    clicks           TEXT,
    conversion_rate  TEXT,
    card_order_count TEXT,
    captured_at      TEXT    NOT NULL,          -- ISO datetime
    industry_name    TEXT    DEFAULT '',        -- 一级类目名（如 智能家居）
    category_name    TEXT    DEFAULT '',        -- 二级类目名（如 五金）
    category_l3_name TEXT    DEFAULT '',        -- 三级类目名（商品叶子类目往上数第3层）
    leaf_category_name TEXT  DEFAULT '',        -- 叶子（最细）类目名
    UNIQUE (run_id, scope_key, product_id)
);

CREATE INDEX IF NOT EXISTS idx_snapshot_run
    ON products_snapshot (run_id, scope_key);

-- 差分事件表
CREATE TABLE IF NOT EXISTS ranking_event (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         TEXT    NOT NULL,            -- 本轮 run_id
    scope_key      TEXT    NOT NULL,
    event_type     TEXT    NOT NULL,            -- NEW_ENTRY / RANK_UP_50 / RANK_UP_100 / RANK_UP_150
    product_id     TEXT    NOT NULL,
    product_title  TEXT,
    product_url    TEXT,
    rank_current   INTEGER,
    rank_previous  INTEGER,
    rank_delta     INTEGER,                     -- 正数 = 上升
    image          TEXT    DEFAULT '',          -- 商品主图 URL
    pay_amount     TEXT    DEFAULT '',          -- 支付金额（脱敏区间，来自快照）
    price          TEXT    DEFAULT '',          -- 实际价格（异动商品详情页拓价，回退价格带）
    created_at     TEXT    NOT NULL,
    notified       INTEGER NOT NULL DEFAULT 0,  -- 0=待推送 1=已推送
    industry_name  TEXT    DEFAULT '',          -- 一级类目名
    category_name  TEXT    DEFAULT '',          -- 二级类目名
    category_l3_name TEXT  DEFAULT '',          -- 三级类目名
    leaf_category_name TEXT DEFAULT '',         -- 叶子（最细）类目名
    UNIQUE (run_id, scope_key, event_type, product_id)   -- 去重防重入
);

CREATE INDEX IF NOT EXISTS idx_event_notified
    ON ranking_event (notified, created_at);
