-- Onyx Binary Signal Bot — Supabase schema (Postgres)
-- Source of truth for the database. Apply in the Supabase SQL Editor
-- (Dashboard -> SQL Editor -> New query -> paste -> Run), or via psql.
-- Free tier only; no paid extension required.
--
-- SCOPE (Nov 24, owner decision): REAL PAIRS ONLY for now.
-- OTC is "coming soon". The `market` column stays (real/otc) so the
-- schema does not change later, but the engine writes real-market
-- signals only in this phase and the API never serves OTC signals.
--
-- Beta mode: all signals served by the API are beta/test data until a
-- strategy proves it wins (see server/plans.py). No accuracy claim
-- is stored or served anywhere in this schema.

-- ---------------------------------------------------------------- users
create table if not exists public.users (
    id               bigint generated always as identity primary key,
    telegram_id      bigint not null unique,
    username         text,
    plan             text not null default 'free'
                     check (plan in ('free', 'premium')),
    premium_until    date,  -- premium is an EXPIRY DATE, not a switch.
                            -- NULL = never had premium / expired.
    signals_used_today integer not null default 0,
    last_signal_date date,  -- day signals_used_today belongs to; reset on new day
    status           text not null default 'active'
                     check (status in ('active', 'blocked')),
    created_at       timestamptz not null default now(),
    updated_at       timestamptz not null default now()
);
comment on table public.users is 'One row per Telegram user. plan=premium only while premium_until >= today.';
comment on column public.users.premium_until is 'Premium expiry DATE. 30 days from payment approval. NULL when free/expired.';
comment on column public.users.signals_used_today is 'Free-plan signals served today; resets when last_signal_date changes.';

-- -------------------------------------------------------------- signals
create table if not exists public.signals (
    id         bigint generated always as identity primary key,
    pair       text not null,                      -- e.g. 'EUR/USD'
    broker     text not null
               check (broker in ('quotex', 'olymp_trade', 'pocket_option', 'finnhub')),
    strategy   text not null
               check (strategy in ('elite', 'turbo', 'prime', 'ha_fade', 'momentum')),
    direction  text not null check (direction in ('call', 'put')),
    entry      timestamptz not null,              -- entry time
    expiry     text not null,                     -- e.g. '1 min' / '5 min' / '15 min'
    sent_at    timestamptz not null default now(),
    result     text not null default 'pending'
               check (result in ('win', 'loss', 'pending', 'void')),
    market     text not null default 'real'
               check (market in ('real', 'otc')),
    is_beta    boolean not null default true,     -- beta/test data until a strategy proves it wins
    created_at timestamptz not null default now()
);
comment on table public.signals is 'Every signal ever sent, kept forever — the honest Results record. Beta until proven.';
comment on column public.signals.market is 'real = real market, otc = broker OTC. SCOPE Nov 24: real only for now; OTC coming soon. Column kept for later.';
comment on column public.signals.result is 'Set to win/loss by the result checker after expiry; never edited after posting.';

-- ------------------------------------------------------------- payments
create table if not exists public.payments (
    id         bigint generated always as identity primary key,
    user_id    bigint not null references public.users(id) on delete cascade,
    network    text not null check (network in ('BSC', 'Tron')),
    amount     numeric(10, 2) not null check (amount = 1.97),
    txid       text not null unique,              -- UNIQUE: one TXID can only ever be used once (no double-spend)
    status     text not null default 'pending'
               check (status in ('pending', 'approved', 'rejected')),
    created_at timestamptz not null default now(),
    decided_at timestamptz,
    decided_by text,                              -- 'manual' or 'auto' (auto-check is opt-in only)
    note       text
);
comment on table public.payments is 'Premium payments, USDT only, 1.97 USDT. txid UNIQUE prevents any TXID being reused by anyone.';
comment on column public.payments.amount is 'Must be exactly 1.97 USDT on either network.';

-- ------------------------------------------------------------- settings
create table if not exists public.settings (
    key        text primary key,
    value      text not null,
    updated_at timestamptz not null default now()
);
comment on table public.settings is 'Live config changeable without touching code: wallet addresses, price, free daily limit.';

-- Seed settings. PLACEHOLDER wallet addresses — replace with the owner's
-- real BSC (BEP20) and Tron (TRC20) USDT addresses before launch.
-- Premium checkout stays DISABLED until these are real (see server README).
insert into public.settings (key, value) values
    ('wallet_bsc',  '0xPLACEHOLDER_BSC — replace with real address before launch'),
    ('wallet_tron', 'PLACEHOLDER_TRON — replace with real address before launch'),
    ('premium_price_usdt', '1.97'),
    ('premium_days', '30'),
    ('free_daily_limit', '10'),
    ('checkout_enabled', 'false'),
    ('otc_enabled', 'false')
on conflict (key) do nothing;

-- ---------------------------------------------------------------- indexes
create index if not exists idx_signals_sent_at   on public.signals (sent_at desc);
create index if not exists idx_signals_broker    on public.signals (broker, sent_at desc);
create index if not exists idx_signals_strategy  on public.signals (strategy, sent_at desc);
create index if not exists idx_signals_market    on public.signals (market, sent_at desc);
create index if not exists idx_signals_result    on public.signals (result, sent_at desc);
create index if not exists idx_payments_user     on public.payments (user_id, created_at desc);
create index if not exists idx_payments_status   on public.payments (status, created_at desc);
-- txid uniqueness is enforced by the UNIQUE constraint above; this index
-- is only for fast duplicate lookups.
create index if not exists idx_payments_txid     on public.payments (txid);

-- ------------------------------------------------- updated_at maintenance
create or replace function public.set_updated_at()
returns trigger language plpgsql as $$
begin
    new.updated_at = now();
    return new;
end;
$$;

drop trigger if exists trg_users_updated_at on public.users;
create trigger trg_users_updated_at
    before update on public.users
    for each row execute function public.set_updated_at();

drop trigger if exists trg_settings_updated_at on public.settings;
create trigger trg_settings_updated_at
    before update on public.settings
    for each row execute function public.set_updated_at();
