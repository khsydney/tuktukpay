-- TukTukPay ledger schema. Runs automatically on first start of the postgres container.

CREATE TABLE IF NOT EXISTS merchants (
    id        text PRIMARY KEY,
    name      text NOT NULL,
    country   text NOT NULL,
    segment   text NOT NULL,
    tier      text NOT NULL DEFAULT 'standard',
    mcc       text
);

CREATE TABLE IF NOT EXISTS payments (
    payment_id          text PRIMARY KEY,
    merchant_id         text NOT NULL,
    order_id            text,
    amount              numeric(14,2) NOT NULL DEFAULT 0,
    currency            text,
    payment_method      text,
    status              text NOT NULL,
    acquirer            text,
    auth_code           text,
    decline_reason      text,
    initiator           text NOT NULL DEFAULT 'human',
    card_bin            text,
    card_network        text,
    customer_country    text,
    risk_score          numeric(6,4),
    risk_model_version  text,
    created_at          timestamptz NOT NULL DEFAULT now(),
    updated_at          timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS payments_merchant_created_idx ON payments (merchant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS payments_status_idx ON payments (status);

CREATE TABLE IF NOT EXISTS ledger_entries (
    id          bigserial PRIMARY KEY,
    payment_id  text NOT NULL REFERENCES payments (payment_id) ON DELETE CASCADE,
    account     text NOT NULL,
    direction   text NOT NULL CHECK (direction IN ('debit', 'credit')),
    amount      numeric(14,2) NOT NULL,
    currency    text,
    created_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS ledger_entries_payment_idx ON ledger_entries (payment_id);

INSERT INTO merchants (id, name, country, segment, tier, mcc) VALUES
    ('thai-airways',    'Thai Airways',                   'TH', 'airline',       'enterprise', '3077'),
    ('lazada-th',       'Lazada Thailand (marketplace)',  'TH', 'ecommerce',     'enterprise', '5399'),
    ('grab',            'Grab (ride hailing)',            'TH', 'mobility',      'enterprise', '4121'),
    ('cafe-amazon',     'Café Amazon (F&B chain)',        'TH', 'fnb',           'sme',        '5814'),
    ('centara-hotels',  'Centara Hotels & Resorts',       'TH', 'hospitality',   'standard',   '7011'),
    ('garena',          'Garena (digital goods)',         'TH', 'digital_goods', 'standard',   '5816')
ON CONFLICT (id) DO NOTHING;
