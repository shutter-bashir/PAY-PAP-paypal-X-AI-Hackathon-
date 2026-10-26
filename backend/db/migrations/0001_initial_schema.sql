-- =====================================================================
-- Pay pap: 0001 initial schema
-- Target: Supabase Postgres 15+. Apply once, either by pasting into the
-- Supabase SQL editor or with:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f backend/db/migrations/0001_initial_schema.sql
--
-- Design notes are in docs/database.md. In short:
--   * Rules flag rows, the agent explains flags, a human decides.
--   * Only approve_batch() can lock rows for payment, and only
--     claim_batch_for_submit() can start a PayPal call. Both run as one
--     transaction, because the Python Supabase client has none of its own.
--   * sender_batch_id is fixed when a batch is created and never changes,
--     so every retry of a submission is deduplicated by PayPal.
--   * audit_log is append-only and hash-chained; triggers write to it, so
--     no code path can skip it.
-- =====================================================================

begin;

-- ---------------------------------------------------------------------
-- Helpers
-- ---------------------------------------------------------------------

-- PayPal item statuses after which the money did not reach the recipient.
create function is_failed_payout_status(p_status text) returns boolean
language sql immutable
as $$ select p_status in ('FAILED', 'RETURNED', 'BLOCKED', 'REFUNDED', 'REVERSED') $$;

create function batch_transition_allowed(p_from text, p_to text) returns boolean
language sql immutable
as $$
    select (p_from, p_to) in (
        ('draft', 'in_review'), ('draft', 'cancelled'),
        ('in_review', 'draft'), ('in_review', 'approved'), ('in_review', 'cancelled'),
        ('approved', 'in_review'), ('approved', 'submitting'),
        ('submitting', 'submitted'), ('submitting', 'rejected'),
        ('submitted', 'processing'), ('submitted', 'completed'), ('submitted', 'rejected'),
        ('processing', 'completed'), ('processing', 'rejected'))
$$;

create function set_updated_at() returns trigger
language plpgsql
as $$
begin
    new.updated_at := now();
    return new;
end $$;

-- ---------------------------------------------------------------------
-- Users: the demo admin (uploads and reviews) and the approver.
-- Passwords stay in backend/.env; this table gives them stable ids.
-- ---------------------------------------------------------------------
create table app_users (
    id           uuid primary key default gen_random_uuid(),
    email        text not null,
    display_name text not null,
    role         text not null check (role in ('admin', 'approver')),
    created_at   timestamptz not null default now()
);
create unique index app_users_email_key on app_users (lower(email));

-- ---------------------------------------------------------------------
-- Recipients: the contractor directory. CSV imports upsert into it.
-- ---------------------------------------------------------------------
create table recipients (
    id             uuid primary key default gen_random_uuid(),
    receiver_type  text not null default 'EMAIL' check (receiver_type in ('EMAIL', 'PHONE', 'PAYPAL_ID')),
    receiver       text not null check (receiver <> '' and receiver = btrim(receiver)),
    full_name      text,
    country_code   char(2) check (country_code ~ '^[A-Z]{2}$'),
    vetting_status text not null default 'pending' check (vetting_status in ('approved', 'pending', 'blocked')),
    external_ref   text,
    notes          text,
    created_at     timestamptz not null default now(),
    updated_at     timestamptz not null default now(),
    constraint recipients_receiver_key unique (receiver_type, receiver),
    constraint recipients_email_lowercase check (receiver_type <> 'EMAIL' or receiver = lower(receiver))
);
create unique index recipients_external_ref_key on recipients (external_ref) where external_ref is not null;
create trigger recipients_updated_at before update on recipients
    for each row execute function set_updated_at();

comment on column recipients.vetting_status is
    'approved = cleared to be paid; pending = not yet checked (warning); blocked = never pay (blocking flag).';

-- ---------------------------------------------------------------------
-- Batches: one uploaded CSV, from draft to PayPal completion.
-- ---------------------------------------------------------------------
create table batches (
    id                     uuid primary key default gen_random_uuid(),
    batch_no               bigint generated always as identity unique,
    name                   text not null,
    status                 text not null default 'draft' check (status in (
                               'draft', 'in_review', 'approved', 'submitting',
                               'submitted', 'processing', 'completed', 'rejected', 'cancelled')),
    currency               char(3) not null default 'USD' check (currency ~ '^[A-Z]{3}$'),
    sender_batch_id        text not null unique
                               default ('paypap_' || replace(gen_random_uuid()::text, '-', '')),
    email_subject          text,
    email_message          text,
    source_filename        text,
    source_sha256          text check (source_sha256 ~ '^[0-9a-f]{64}$'),
    column_mapping         jsonb,
    import_errors          jsonb not null default '[]'::jsonb,
    rules_version          text,
    validated_at           timestamptz,
    ai_summary             jsonb,
    ai_model               text,
    ai_summarized_at       timestamptz,
    paypal_payout_batch_id text unique,
    paypal_batch_status    text,
    submitted_at           timestamptz,
    completed_at           timestamptz,
    last_polled_at         timestamptz,
    created_by             uuid not null references app_users (id),
    created_at             timestamptz not null default now(),
    updated_at             timestamptz not null default now()
);
create index batches_status_idx on batches (status);
create index batches_source_sha256_idx on batches (source_sha256);
create trigger batches_updated_at before update on batches
    for each row execute function set_updated_at();

comment on column batches.sender_batch_id is
    'Sent to PayPal as sender_batch_id. Fixed at creation; every retry reuses it, so PayPal pays a batch at most once.';
comment on column batches.import_errors is
    'File-level and row-level problems found while parsing the CSV, as [{row, column, message}].';

-- ---------------------------------------------------------------------
-- Batch items: one CSV row. Bad rows are stored too, so they can be
-- flagged and shown; nothing here is trusted until approve_batch() runs.
-- The row id is sent to PayPal as sender_item_id.
-- ---------------------------------------------------------------------
create table batch_items (
    id                    uuid primary key default gen_random_uuid(),
    batch_id              uuid not null references batches (id) on delete cascade,
    row_number            integer not null check (row_number > 0),
    raw                   jsonb not null,
    recipient_id          uuid references recipients (id),
    receiver_type         text not null default 'EMAIL' check (receiver_type in ('EMAIL', 'PHONE', 'PAYPAL_ID')),
    receiver              text,
    recipient_name        text,
    amount                numeric(12, 2),
    currency              char(3),
    invoice_id            text,
    note                  text,
    decision              text not null default 'pending' check (decision in ('pending', 'include', 'exclude')),
    decision_reason       text,
    decided_by            uuid references app_users (id),
    decided_at            timestamptz,
    committed_at          timestamptz,
    paypal_payout_item_id text unique,
    paypal_transaction_id text,
    payout_status         text,
    payout_status_source  text check (payout_status_source in ('submit', 'webhook', 'poll')),
    payout_status_at      timestamptz,
    payout_error          jsonb,
    payout_fee            numeric(12, 2),
    created_at            timestamptz not null default now(),
    updated_at            timestamptz not null default now(),
    unique (batch_id, row_number),
    unique (id, batch_id),
    constraint batch_items_email_lowercase check (receiver_type <> 'EMAIL' or receiver = lower(receiver)),
    constraint batch_items_committed_is_payable check (
        committed_at is null
        or (decision = 'include' and receiver is not null and amount > 0 and currency is not null))
);
create index batch_items_batch_decision_idx on batch_items (batch_id, decision);
create index batch_items_recipient_idx on batch_items (recipient_id);
create index batch_items_invoice_idx on batch_items (lower(invoice_id));

-- Backstop against paying one invoice twice, across all batches. A row
-- holds its invoice from approval until its payout fails or its batch is
-- rejected, cancelled or sent back to review.
create unique index batch_items_invoice_paid_once
    on batch_items (receiver_type, receiver, lower(invoice_id))
    where committed_at is not null and invoice_id is not null
      and not coalesce(is_failed_payout_status(payout_status), false);

comment on column batch_items.decision is
    'Set by the rules engine (decided_by null) or a person. Warned rows stay pending until a person decides.';
comment on column batch_items.committed_at is
    'Set by approve_batch(). While set, the payment fields and the decision cannot change.';
comment on column batch_items.payout_status_at is
    'PayPal time of the event that set payout_status; older events are ignored, so out-of-order webhooks are harmless.';

-- ---------------------------------------------------------------------
-- Flags: rule results. batch_item_id null means a batch-level flag.
-- The rules engine writes them; the agent only adds the explanation.
-- ---------------------------------------------------------------------
create table flags (
    id              uuid primary key default gen_random_uuid(),
    batch_id        uuid not null references batches (id) on delete cascade,
    batch_item_id   uuid,
    code            text not null check (code ~ '^[A-Z][A-Z0-9_]*$'),
    severity        text not null check (severity in ('info', 'warning', 'blocking')),
    message         text not null,
    evidence        jsonb not null default '{}'::jsonb,
    rule_version    text not null,
    ai_explanation  text,
    ai_model        text,
    ai_explained_at timestamptz,
    created_at      timestamptz not null default now(),
    foreign key (batch_item_id, batch_id) references batch_items (id, batch_id) on delete cascade,
    constraint flags_one_per_code unique nulls not distinct (batch_id, batch_item_id, code)
);
create index flags_item_idx on flags (batch_item_id);

-- ---------------------------------------------------------------------
-- Approvals: the gate. The API creates the id and the HMAC signature
-- (APPROVAL_SIGNING_SECRET) before calling approve_batch(); submit
-- re-checks the signature, so a hand-inserted row cannot release money.
-- ---------------------------------------------------------------------
create table approvals (
    id              uuid primary key,
    batch_id        uuid not null references batches (id),
    approved_by     uuid not null references app_users (id),
    sender_batch_id text not null,
    item_count      integer not null check (item_count > 0),
    total_amount    numeric(14, 2) not null check (total_amount > 0),
    currency        char(3) not null,
    excluded_count  integer not null check (excluded_count >= 0),
    warned_count    integer not null check (warned_count >= 0),
    signature       text not null check (signature ~ '^[0-9a-f]{64}$'),
    comment         text,
    expires_at      timestamptz not null,
    created_at      timestamptz not null default now(),
    consumed_at     timestamptz,
    revoked_at      timestamptz,
    revoked_by      uuid references app_users (id),
    revoke_reason   text,
    check (expires_at > created_at),
    check (revoked_at is null or revoke_reason is not null)
);
create unique index approvals_one_live_per_batch on approvals (batch_id) where revoked_at is null;

-- ---------------------------------------------------------------------
-- Payout submissions: one row per call to POST /v1/payments/payouts.
-- 'unknown' means the call timed out or the connection dropped; retry
-- with the same sender_batch_id.
-- ---------------------------------------------------------------------
create table payout_submissions (
    id                     uuid primary key default gen_random_uuid(),
    batch_id               uuid not null references batches (id),
    approval_id            uuid not null references approvals (id),
    attempt_no             integer not null check (attempt_no > 0),
    sender_batch_id        text not null,
    submitted_by           uuid references app_users (id),
    outcome                text not null default 'in_flight'
                               check (outcome in ('in_flight', 'accepted', 'rejected', 'unknown')),
    http_status            integer,
    paypal_debug_id        text,
    paypal_payout_batch_id text,
    request_body           jsonb,
    response_body          jsonb,
    error                  text,
    started_at             timestamptz not null default now(),
    finished_at            timestamptz,
    unique (batch_id, attempt_no),
    check ((outcome = 'in_flight') = (finished_at is null))
);
create unique index payout_submissions_one_in_flight on payout_submissions (batch_id) where outcome = 'in_flight';
create unique index payout_submissions_one_accepted on payout_submissions (batch_id) where outcome = 'accepted';

-- ---------------------------------------------------------------------
-- Webhook events: only signature-verified events are stored. The unique
-- event id makes redelivery a no-op (insert ... on conflict do nothing).
-- ---------------------------------------------------------------------
create table webhook_events (
    id               uuid primary key default gen_random_uuid(),
    paypal_event_id  text not null unique,
    event_type       text not null,
    resource_type    text,
    payout_batch_id  text,
    sender_item_id   text,
    event_created_at timestamptz,
    payload          jsonb not null,
    received_at      timestamptz not null default now(),
    processed_at     timestamptz,
    processing_error text
);
create index webhook_events_payout_batch_idx on webhook_events (payout_batch_id);

-- ---------------------------------------------------------------------
-- Audit log: append-only, hash-chained. id is gap-free and assigned under
-- a lock, so verify_audit_chain() can replay the chain in id order.
-- ---------------------------------------------------------------------
create table audit_log (
    id          bigint primary key,
    occurred_at timestamptz not null default now(),
    actor_type  text not null check (actor_type in ('user', 'system', 'agent', 'paypal')),
    actor_id    uuid references app_users (id),
    action      text not null check (action ~ '^[a-z_]+(\.[a-z_]+)+$'),
    batch_id    uuid,
    entity_type text not null,
    entity_id   text,
    details     jsonb not null default '{}'::jsonb,
    prev_hash   bytea,
    hash        bytea not null,
    check (actor_type <> 'user' or actor_id is not null)
);
create index audit_log_batch_idx on audit_log (batch_id, id);
create index audit_log_entity_idx on audit_log (entity_type, entity_id);

create function audit_hash(p_prev bytea, p_row audit_log) returns bytea
language sql stable
as $$
    select sha256(coalesce(p_prev, ''::bytea) || convert_to(jsonb_build_object(
        'id', p_row.id,
        'occurred_at', to_char(p_row.occurred_at at time zone 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
        'actor_type', p_row.actor_type,
        'actor_id', p_row.actor_id,
        'action', p_row.action,
        'batch_id', p_row.batch_id,
        'entity_type', p_row.entity_type,
        'entity_id', p_row.entity_id,
        'details', p_row.details)::text, 'UTF8'))
$$;

create function audit_log_chain() returns trigger
language plpgsql
as $$
declare
    v_last audit_log%rowtype;
begin
    perform pg_advisory_xact_lock(hashtext('paypap.audit_log'));
    select * into v_last from audit_log order by id desc limit 1;
    new.id := coalesce(v_last.id, 0) + 1;
    new.occurred_at := now();
    new.prev_hash := v_last.hash;
    new.hash := audit_hash(new.prev_hash, new);
    return new;
end $$;

create function audit_log_block_change() returns trigger
language plpgsql
as $$
begin
    raise exception 'audit_log is append-only (% refused)', tg_op;
end $$;

create trigger audit_log_chain before insert on audit_log
    for each row execute function audit_log_chain();
create trigger audit_log_no_update before update or delete on audit_log
    for each row execute function audit_log_block_change();
create trigger audit_log_no_truncate before truncate on audit_log
    for each statement execute function audit_log_block_change();

-- Also callable with supabase.rpc() for events no table change captures
-- (imports, AI summaries of filters, logins, rejected webhooks).
create function write_audit(
    p_actor_type  text,
    p_actor_id    uuid,
    p_action      text,
    p_entity_type text,
    p_entity_id   text,
    p_batch_id    uuid default null,
    p_details     jsonb default '{}'::jsonb
) returns bigint
language sql
as $$
    insert into audit_log (actor_type, actor_id, action, batch_id, entity_type, entity_id, details)
    values (p_actor_type, p_actor_id, p_action, p_batch_id, p_entity_type, p_entity_id, coalesce(p_details, '{}'::jsonb))
    returning id
$$;

-- Returns how many rows check out and the first id that does not (null if intact).
create function verify_audit_chain() returns table (rows_checked bigint, first_bad_id bigint)
language plpgsql stable
as $$
declare
    r          audit_log%rowtype;
    v_prev     bytea;
    v_expected bigint := 1;
begin
    rows_checked := 0;
    for r in select * from audit_log order by id loop
        if r.id <> v_expected or r.prev_hash is distinct from v_prev or r.hash <> audit_hash(v_prev, r) then
            first_bad_id := r.id;
            return next;
            return;
        end if;
        v_prev := r.hash;
        v_expected := v_expected + 1;
        rows_checked := rows_checked + 1;
    end loop;
    return next;
end $$;

-- ---------------------------------------------------------------------
-- Guards and audit triggers
-- ---------------------------------------------------------------------

create function batches_guard() returns trigger
language plpgsql
as $$
begin
    if new.sender_batch_id is distinct from old.sender_batch_id then
        raise exception 'sender_batch_id is fixed for the life of a batch; create a new batch instead';
    end if;
    if new.created_by is distinct from old.created_by then
        raise exception 'created_by cannot change';
    end if;
    if new.status is distinct from old.status then
        if not batch_transition_allowed(old.status, new.status) then
            raise exception 'batch cannot move from % to %', old.status, new.status;
        end if;
        -- Money states need their evidence row, so a bare UPDATE cannot fake them.
        if new.status = 'approved'
           and not exists (select 1 from approvals where batch_id = new.id and revoked_at is null) then
            raise exception 'batch can only become approved through approve_batch()';
        end if;
        if new.status = 'submitting'
           and not exists (select 1 from payout_submissions where batch_id = new.id and outcome = 'in_flight') then
            raise exception 'batch can only become submitting through claim_batch_for_submit()';
        end if;
        if new.status = 'submitted'
           and not exists (select 1 from payout_submissions where batch_id = new.id and outcome = 'accepted') then
            raise exception 'batch can only become submitted after PayPal accepts it';
        end if;
        if new.status = 'submitted' then
            new.submitted_at := coalesce(new.submitted_at, now());
        elsif new.status in ('completed', 'rejected', 'cancelled') then
            new.completed_at := coalesce(new.completed_at, now());
        end if;
    end if;
    return new;
end $$;

create trigger batches_guard before update on batches
    for each row execute function batches_guard();

create function batches_after_change() returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT' then
        perform write_audit('user', new.created_by, 'batch.created', 'batch', new.id::text, new.id,
            jsonb_build_object('name', new.name, 'source_filename', new.source_filename,
                               'sender_batch_id', new.sender_batch_id));
        return null;
    end if;
    if new.status is distinct from old.status then
        -- Leaving the approved path releases the invoice locks.
        if new.status in ('in_review', 'rejected', 'cancelled') then
            update batch_items set committed_at = null where batch_id = new.id and committed_at is not null;
        end if;
        perform write_audit('system', null, 'batch.status_changed', 'batch', new.id::text, new.id,
            jsonb_build_object('from', old.status, 'to', new.status, 'paypal_batch_status', new.paypal_batch_status));
    end if;
    if new.ai_summary is distinct from old.ai_summary and new.ai_summary is not null then
        perform write_audit('agent', null, 'batch.summarized', 'batch', new.id::text, new.id,
            jsonb_build_object('model', new.ai_model));
    end if;
    return null;
end $$;

create trigger batches_after_change after insert or update on batches
    for each row execute function batches_after_change();

create function batch_items_guard() returns trigger
language plpgsql
as $$
declare
    v_status text;
begin
    select status into v_status from batches where id = new.batch_id;
    if tg_op = 'INSERT' then
        if v_status <> 'draft' then
            raise exception 'rows can only be added to a draft batch (this batch is %)', v_status;
        end if;
    else
        if new.batch_id <> old.batch_id or new.row_number <> old.row_number then
            raise exception 'batch_id and row_number cannot change';
        end if;
        if old.committed_at is not null and new.committed_at is not null
           and (new.receiver_type, new.receiver, new.amount, new.currency, new.invoice_id, new.decision)
               is distinct from (old.receiver_type, old.receiver, old.amount, old.currency, old.invoice_id, old.decision) then
            raise exception 'row % is locked by an approval; revoke the approval to change it', new.row_number;
        end if;
    end if;

    if tg_op = 'INSERT' or new.decision is distinct from old.decision then
        if v_status not in ('draft', 'in_review') then
            raise exception 'decisions can only change while the batch is draft or in_review (this batch is %)', v_status;
        end if;
        if new.decision = 'include'
           and exists (select 1 from flags where batch_item_id = new.id and severity = 'blocking') then
            raise exception 'row % has a blocking flag and cannot be included', new.row_number;
        end if;
        new.decided_at := case when new.decision = 'pending' then null else now() end;
    end if;
    return new;
end $$;

create trigger batch_items_guard before insert or update on batch_items
    for each row execute function batch_items_guard();
create trigger batch_items_updated_at before update on batch_items
    for each row execute function set_updated_at();

create function batch_items_after_change() returns trigger
language plpgsql
as $$
begin
    if (tg_op = 'INSERT' and new.decision <> 'pending')
       or (tg_op = 'UPDATE' and new.decision is distinct from old.decision) then
        perform write_audit(
            case when new.decided_by is null then 'system' else 'user' end, new.decided_by,
            'item.decision_changed', 'batch_item', new.id::text, new.batch_id,
            jsonb_build_object('row', new.row_number,
                               'from', case when tg_op = 'UPDATE' then old.decision end,
                               'to', new.decision, 'reason', new.decision_reason));
    end if;
    if tg_op = 'UPDATE' and new.payout_status is distinct from old.payout_status then
        perform write_audit(
            case when new.payout_status_source = 'submit' then 'system' else 'paypal' end, null,
            'item.payout_status_changed', 'batch_item', new.id::text, new.batch_id,
            jsonb_build_object('row', new.row_number, 'from', old.payout_status, 'to', new.payout_status,
                               'source', new.payout_status_source,
                               'paypal_transaction_id', new.paypal_transaction_id));
    end if;
    return null;
end $$;

create trigger batch_items_after_change after insert or update on batch_items
    for each row execute function batch_items_after_change();

create function flags_guard() returns trigger
language plpgsql
as $$
declare
    v_status text;
begin
    if tg_op = 'INSERT' then
        select status into v_status from batches where id = new.batch_id;
        if v_status not in ('draft', 'in_review') then
            raise exception 'flags can only be raised while the batch is draft or in_review (this batch is %)', v_status;
        end if;
    elsif (new.batch_id, new.batch_item_id, new.code, new.severity, new.message, new.evidence, new.rule_version, new.created_at)
          is distinct from (old.batch_id, old.batch_item_id, old.code, old.severity, old.message, old.evidence, old.rule_version, old.created_at) then
        raise exception 'flags are written by the rules engine; only the AI explanation can be added';
    end if;
    return new;
end $$;

create trigger flags_guard before insert or update on flags
    for each row execute function flags_guard();

create function flags_after_change() returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT' then
        perform write_audit('system', null, 'flag.raised', 'flag', new.id::text, new.batch_id,
            jsonb_build_object('code', new.code, 'severity', new.severity, 'batch_item_id', new.batch_item_id,
                               'message', new.message, 'rule_version', new.rule_version));
    elsif tg_op = 'DELETE' then
        perform write_audit('system', null, 'flag.cleared', 'flag', old.id::text, old.batch_id,
            jsonb_build_object('code', old.code, 'batch_item_id', old.batch_item_id));
    elsif new.ai_explanation is distinct from old.ai_explanation then
        perform write_audit('agent', null, 'flag.explained', 'flag', new.id::text, new.batch_id,
            jsonb_build_object('code', new.code, 'model', new.ai_model));
    end if;
    return null;
end $$;

create trigger flags_after_change after insert or update or delete on flags
    for each row execute function flags_after_change();

create function approvals_guard() returns trigger
language plpgsql
as $$
begin
    if tg_op = 'DELETE' then
        raise exception 'approvals cannot be deleted; revoke them instead';
    end if;
    if (new.id, new.batch_id, new.approved_by, new.sender_batch_id, new.item_count, new.total_amount,
        new.currency, new.excluded_count, new.warned_count, new.signature, new.comment, new.expires_at, new.created_at)
       is distinct from
       (old.id, old.batch_id, old.approved_by, old.sender_batch_id, old.item_count, old.total_amount,
        old.currency, old.excluded_count, old.warned_count, old.signature, old.comment, old.expires_at, old.created_at)
       or (old.consumed_at is not null and new.consumed_at is distinct from old.consumed_at)
       or (old.revoked_at is not null and (new.revoked_at, new.revoked_by, new.revoke_reason)
                                          is distinct from (old.revoked_at, old.revoked_by, old.revoke_reason)) then
        raise exception 'an approval is immutable; only consumed_at and the revoke fields can be set, once';
    end if;
    return new;
end $$;

create trigger approvals_guard before update or delete on approvals
    for each row execute function approvals_guard();

create function approvals_after_change() returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT' then
        perform write_audit('user', new.approved_by, 'batch.approved', 'approval', new.id::text, new.batch_id,
            jsonb_build_object('item_count', new.item_count, 'total_amount', new.total_amount,
                               'currency', new.currency, 'excluded_count', new.excluded_count,
                               'warned_count', new.warned_count, 'expires_at', new.expires_at,
                               'comment', new.comment));
    elsif new.revoked_at is not null and old.revoked_at is null then
        perform write_audit(case when new.revoked_by is null then 'system' else 'user' end, new.revoked_by,
            'approval.revoked', 'approval', new.id::text, new.batch_id,
            jsonb_build_object('reason', new.revoke_reason));
    end if;
    return null;
end $$;

create trigger approvals_after_change after insert or update on approvals
    for each row execute function approvals_after_change();

create function payout_submissions_after_change() returns trigger
language plpgsql
as $$
begin
    if tg_op = 'INSERT' then
        perform write_audit(case when new.submitted_by is null then 'system' else 'user' end, new.submitted_by,
            'payout.submit_started', 'payout_submission', new.id::text, new.batch_id,
            jsonb_build_object('attempt_no', new.attempt_no, 'sender_batch_id', new.sender_batch_id,
                               'approval_id', new.approval_id));
    elsif new.outcome is distinct from old.outcome then
        perform write_audit('system', null, 'payout.submit_' || new.outcome, 'payout_submission', new.id::text, new.batch_id,
            jsonb_build_object('attempt_no', new.attempt_no, 'http_status', new.http_status,
                               'paypal_debug_id', new.paypal_debug_id,
                               'paypal_payout_batch_id', new.paypal_payout_batch_id, 'error', new.error));
    end if;
    return null;
end $$;

create trigger payout_submissions_after_change after insert or update on payout_submissions
    for each row execute function payout_submissions_after_change();

create function webhook_events_after_insert() returns trigger
language plpgsql
as $$
begin
    perform write_audit('paypal', null, 'webhook.received', 'webhook_event', new.paypal_event_id,
        (select id from batches where paypal_payout_batch_id = new.payout_batch_id),
        jsonb_build_object('event_type', new.event_type, 'sender_item_id', new.sender_item_id));
    return null;
end $$;

create trigger webhook_events_after_insert after insert on webhook_events
    for each row execute function webhook_events_after_insert();

-- ---------------------------------------------------------------------
-- State-changing functions (call with supabase.rpc)
-- ---------------------------------------------------------------------

-- What the rules engine needs to know about the receivers in a new CSV:
-- which are in the directory, and which invoices they were already paid.
create function import_context(p_receiver_type text, p_receivers text[]) returns jsonb
language sql stable
set search_path = public, pg_temp
as $$
    select jsonb_build_object(
        'recipients', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'id', r.id, 'receiver', r.receiver, 'full_name', r.full_name,
                       'vetting_status', r.vetting_status))
              from recipients r
             where r.receiver_type = p_receiver_type and r.receiver = any (p_receivers)), '[]'::jsonb),
        'paid_invoices', coalesce((
            select jsonb_agg(jsonb_build_object(
                       'receiver', i.receiver, 'invoice_id', i.invoice_id,
                       'batch_no', b.batch_no, 'payout_status', i.payout_status))
              from batch_items i
              join batches b on b.id = i.batch_id
             where i.receiver_type = p_receiver_type and i.receiver = any (p_receivers)
               and i.invoice_id is not null and i.committed_at is not null
               and not coalesce(is_failed_payout_status(i.payout_status), false)), '[]'::jsonb))
$$;

-- Writes a parsed and rule-checked CSV in one transaction: new recipients
-- (as pending vetting), the batch, its rows, the flags, then the rules
-- engine's default decisions. Flags refer to rows by row_number (null for
-- a batch-level flag). Returns the new batch id, already in_review.
create function import_batch(
    p_created_by uuid,
    p_batch      jsonb,
    p_recipients jsonb,
    p_items      jsonb,
    p_flags      jsonb
) returns uuid
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch_id uuid;
    v_orphans  text;
begin
    insert into recipients (receiver_type, receiver, full_name, country_code, external_ref)
    select r.receiver_type, r.receiver, r.full_name, r.country_code, r.external_ref
      from jsonb_to_recordset(p_recipients)
           as r(receiver_type text, receiver text, full_name text, country_code text, external_ref text)
    on conflict do nothing;

    insert into batches (name, currency, source_filename, source_sha256, column_mapping, import_errors,
                         email_subject, email_message, created_by)
    values (p_batch ->> 'name',
            coalesce(p_batch ->> 'currency', 'USD'),
            p_batch ->> 'source_filename',
            p_batch ->> 'source_sha256',
            p_batch -> 'column_mapping',
            coalesce(p_batch -> 'import_errors', '[]'::jsonb),
            p_batch ->> 'email_subject',
            p_batch ->> 'email_message',
            p_created_by)
    returning id into v_batch_id;

    insert into batch_items (batch_id, row_number, raw, recipient_id, receiver_type, receiver, recipient_name,
                             amount, currency, invoice_id, note)
    select v_batch_id, it.row_number, it.raw, rc.id, coalesce(it.receiver_type, 'EMAIL'), it.receiver,
           it.recipient_name, it.amount, it.currency, it.invoice_id, it.note
      from jsonb_to_recordset(p_items)
           as it(row_number integer, raw jsonb, receiver_type text, receiver text, recipient_name text,
                 amount numeric, currency text, invoice_id text, note text)
      left join recipients rc
             on rc.receiver_type = coalesce(it.receiver_type, 'EMAIL') and rc.receiver = it.receiver;

    select string_agg(f.row_number::text, ', ') into v_orphans
      from jsonb_to_recordset(p_flags) as f(row_number integer)
     where f.row_number is not null
       and not exists (select 1 from batch_items i where i.batch_id = v_batch_id and i.row_number = f.row_number);
    if v_orphans is not null then
        raise exception 'flags refer to rows that are not in the batch: %', v_orphans;
    end if;

    insert into flags (batch_id, batch_item_id, code, severity, message, evidence, rule_version)
    select v_batch_id, i.id, f.code, f.severity, f.message, coalesce(f.evidence, '{}'::jsonb), p_batch ->> 'rules_version'
      from jsonb_to_recordset(p_flags)
           as f(row_number integer, code text, severity text, message text, evidence jsonb)
      left join batch_items i on i.batch_id = v_batch_id and i.row_number = f.row_number;

    update batch_items i
       set decision = d.decision, decision_reason = d.decision_reason
      from jsonb_to_recordset(p_items) as d(row_number integer, decision text, decision_reason text)
     where i.batch_id = v_batch_id and i.row_number = d.row_number
       and d.decision is not null and d.decision <> 'pending';

    update batches
       set status = 'in_review', validated_at = now(), rules_version = p_batch ->> 'rules_version'
     where id = v_batch_id;

    perform write_audit('user', p_created_by, 'batch.imported', 'batch', v_batch_id::text, v_batch_id,
        jsonb_build_object('rows', jsonb_array_length(p_items), 'flags', jsonb_array_length(p_flags),
                           'import_errors', jsonb_array_length(coalesce(p_batch -> 'import_errors', '[]'::jsonb)),
                           'rules_version', p_batch ->> 'rules_version'));
    return v_batch_id;
end $$;

-- A person includes or excludes rows. p_decisions: [{row_number, decision, reason}].
-- The row guard refuses including a blocked row or deciding after approval.
create function set_item_decisions(
    p_batch_id   uuid,
    p_decided_by uuid,
    p_decisions  jsonb
) returns integer
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_missing text;
    v_changed integer;
begin
    if p_decided_by is null then
        raise exception 'a person must be named as decided_by';
    end if;
    select string_agg(d.row_number::text, ', ') into v_missing
      from jsonb_to_recordset(p_decisions) as d(row_number integer)
     where not exists (select 1 from batch_items i where i.batch_id = p_batch_id and i.row_number = d.row_number);
    if v_missing is not null then
        raise exception 'rows % are not in this batch', v_missing;
    end if;

    update batch_items i
       set decision = d.decision, decision_reason = d.reason, decided_by = p_decided_by
      from jsonb_to_recordset(p_decisions) as d(row_number integer, decision text, reason text)
     where i.batch_id = p_batch_id and i.row_number = d.row_number
       and (i.decision, i.decided_by) is distinct from (d.decision, p_decided_by);
    get diagnostics v_changed = row_count;
    return v_changed;
end $$;

-- The approver saw p_item_count rows totalling p_total_amount. Refuses if
-- the batch changed since, if any row is undecided, or if an included row
-- is blocked, in the wrong currency or a repeat of an invoice already paid.
create function approve_batch(
    p_approval_id  uuid,
    p_batch_id     uuid,
    p_approver_id  uuid,
    p_item_count   integer,
    p_total_amount numeric,
    p_expires_at   timestamptz,
    p_signature    text,
    p_comment      text default null
) returns approvals
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch    batches%rowtype;
    v_role     text;
    v_count    integer;
    v_total    numeric;
    v_pending  integer;
    v_excluded integer;
    v_warned   integer;
    v_bad      text;
    v_approval approvals%rowtype;
begin
    select * into v_batch from batches where id = p_batch_id for update;
    if not found then
        raise exception 'batch % not found', p_batch_id;
    end if;
    if v_batch.status <> 'in_review' then
        raise exception 'batch is %; only an in_review batch can be approved', v_batch.status;
    end if;

    select role into v_role from app_users where id = p_approver_id;
    if v_role is distinct from 'approver' then
        raise exception 'user % is not an approver', p_approver_id;
    end if;
    if p_approver_id = v_batch.created_by then
        raise exception 'the person who uploaded a batch cannot approve it';
    end if;
    if p_expires_at <= now() or p_expires_at > now() + interval '24 hours' then
        raise exception 'approval expiry must fall within the next 24 hours';
    end if;

    select count(*) filter (where decision = 'include'),
           coalesce(sum(amount) filter (where decision = 'include'), 0),
           count(*) filter (where decision = 'pending'),
           count(*) filter (where decision = 'exclude')
      into v_count, v_total, v_pending, v_excluded
      from batch_items
     where batch_id = p_batch_id;

    if v_pending > 0 then
        raise exception '% row(s) still need an include or exclude decision', v_pending;
    end if;
    if v_count = 0 then
        raise exception 'no rows are included, so there is nothing to pay';
    end if;
    if v_count <> p_item_count or v_total <> p_total_amount then
        raise exception 'batch changed since it was reviewed: approver saw % rows totalling %, batch now has % rows totalling %',
            p_item_count, p_total_amount, v_count, v_total;
    end if;

    select string_agg(distinct f.code, ', ') into v_bad
      from flags f
      left join batch_items i on i.id = f.batch_item_id
     where f.batch_id = p_batch_id and f.severity = 'blocking'
       and (f.batch_item_id is null or i.decision = 'include');
    if v_bad is not null then
        raise exception 'blocking flags are still open on the batch or on included rows: %', v_bad;
    end if;

    select string_agg(row_number::text, ', ' order by row_number) into v_bad
      from batch_items
     where batch_id = p_batch_id and decision = 'include'
       and (receiver is null or amount is null or amount <= 0 or currency is distinct from v_batch.currency);
    if v_bad is not null then
        raise exception 'included rows % are missing a receiver, have no positive amount, or are not in %',
            v_bad, v_batch.currency;
    end if;

    select string_agg(distinct i.invoice_id, ', ') into v_bad
      from batch_items i
     where i.batch_id = p_batch_id and i.decision = 'include' and i.invoice_id is not null
       and exists (
           select 1 from batch_items o
            where o.id <> i.id
              and o.receiver_type = i.receiver_type
              and o.receiver = i.receiver
              and lower(o.invoice_id) = lower(i.invoice_id)
              and ((o.batch_id = p_batch_id and o.decision = 'include')
                   or (o.committed_at is not null and not coalesce(is_failed_payout_status(o.payout_status), false))));
    if v_bad is not null then
        raise exception 'invoice(s) % would be paid twice to the same recipient', v_bad;
    end if;

    select count(distinct i.id) into v_warned
      from batch_items i
      join flags f on f.batch_item_id = i.id and f.severity = 'warning'
     where i.batch_id = p_batch_id and i.decision = 'include';

    insert into approvals (id, batch_id, approved_by, sender_batch_id, item_count, total_amount, currency,
                           excluded_count, warned_count, signature, comment, expires_at)
    values (p_approval_id, p_batch_id, p_approver_id, v_batch.sender_batch_id, v_count, v_total, v_batch.currency,
            v_excluded, v_warned, p_signature, p_comment, p_expires_at)
    returning * into v_approval;

    update batch_items set committed_at = now() where batch_id = p_batch_id and decision = 'include';
    update batches set status = 'approved' where id = p_batch_id;
    return v_approval;
end $$;

-- Sends an approved (not yet submitted) batch back to review and releases
-- its rows. p_revoked_by null means the system did it (e.g. expiry).
create function revoke_approval(
    p_approval_id uuid,
    p_reason      text,
    p_revoked_by  uuid default null
) returns batches
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch_id uuid;
    v_batch    batches%rowtype;
    v_approval approvals%rowtype;
begin
    select batch_id into v_batch_id from approvals where id = p_approval_id;
    if not found then
        raise exception 'approval % not found', p_approval_id;
    end if;
    select * into v_batch from batches where id = v_batch_id for update;
    select * into v_approval from approvals where id = p_approval_id;
    if v_approval.revoked_at is not null then
        raise exception 'approval % is already revoked', p_approval_id;
    end if;
    if v_batch.status <> 'approved' then
        raise exception 'batch is %; an approval can only be revoked before submission', v_batch.status;
    end if;

    update approvals set revoked_at = now(), revoked_by = p_revoked_by, revoke_reason = p_reason
     where id = p_approval_id;
    update batches set status = 'in_review' where id = v_batch_id returning * into v_batch;
    return v_batch;
end $$;

-- The submit gate. Opens a new submission attempt, or refuses. The API
-- must verify the approval's HMAC signature before calling this, then
-- call PayPal with batches.sender_batch_id, then record_submission_result().
-- Retrying from 'submitting' is allowed (the earlier outcome was unknown),
-- and PayPal deduplicates on sender_batch_id.
create function claim_batch_for_submit(
    p_batch_id     uuid,
    p_approval_id  uuid,
    p_submitted_by uuid default null
) returns payout_submissions
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch      batches%rowtype;
    v_approval   approvals%rowtype;
    v_count      integer;
    v_total      numeric;
    v_attempt    integer;
    v_submission payout_submissions%rowtype;
begin
    select * into v_batch from batches where id = p_batch_id for update;
    if not found then
        raise exception 'batch % not found', p_batch_id;
    end if;
    if v_batch.status not in ('approved', 'submitting') then
        raise exception 'batch is %; only an approved batch can be submitted', v_batch.status;
    end if;

    select * into v_approval from approvals where id = p_approval_id and batch_id = p_batch_id;
    if not found or v_approval.revoked_at is not null then
        raise exception 'approval % is not a live approval for this batch', p_approval_id;
    end if;
    if v_batch.status = 'approved' and v_approval.expires_at <= now() then
        raise exception 'approval expired at %; revoke it and approve the batch again', v_approval.expires_at;
    end if;
    if v_approval.sender_batch_id <> v_batch.sender_batch_id then
        raise exception 'approval was given for a different sender_batch_id';
    end if;
    if exists (select 1 from payout_submissions where batch_id = p_batch_id and outcome = 'accepted') then
        raise exception 'PayPal has already accepted this batch';
    end if;

    -- A worker that died mid-call leaves an attempt in flight; after five
    -- minutes treat its outcome as unknown so a retry can go ahead.
    update payout_submissions
       set outcome = 'unknown', finished_at = now(), error = 'abandoned: no result recorded within 5 minutes'
     where batch_id = p_batch_id and outcome = 'in_flight' and started_at < now() - interval '5 minutes';
    if exists (select 1 from payout_submissions where batch_id = p_batch_id and outcome = 'in_flight') then
        raise exception 'a submission for this batch is already in progress';
    end if;

    select count(*), coalesce(sum(amount), 0) into v_count, v_total
      from batch_items
     where batch_id = p_batch_id and committed_at is not null;
    if v_count <> v_approval.item_count or v_total <> v_approval.total_amount then
        raise exception 'locked rows (% totalling %) do not match the approval (% totalling %)',
            v_count, v_total, v_approval.item_count, v_approval.total_amount;
    end if;

    select coalesce(max(attempt_no), 0) + 1 into v_attempt from payout_submissions where batch_id = p_batch_id;
    insert into payout_submissions (batch_id, approval_id, attempt_no, sender_batch_id, submitted_by)
    values (p_batch_id, p_approval_id, v_attempt, v_batch.sender_batch_id, p_submitted_by)
    returning * into v_submission;

    if v_batch.status = 'approved' then
        update approvals set consumed_at = now() where id = p_approval_id;
        update batches set status = 'submitting' where id = p_batch_id;
    end if;
    return v_submission;
end $$;

-- Records what PayPal said. 'accepted' needs the payout_batch_id;
-- 'rejected' is only for a definite refusal of a request PayPal never
-- processed; anything uncertain is 'unknown' and stays retryable.
create function record_submission_result(
    p_submission_id          uuid,
    p_outcome                text,
    p_http_status            integer default null,
    p_paypal_debug_id        text default null,
    p_paypal_payout_batch_id text default null,
    p_paypal_batch_status    text default null,
    p_request_body           jsonb default null,
    p_response_body          jsonb default null,
    p_error                  text default null
) returns batches
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch_id   uuid;
    v_batch      batches%rowtype;
    v_submission payout_submissions%rowtype;
begin
    if p_outcome not in ('accepted', 'rejected', 'unknown') then
        raise exception 'outcome must be accepted, rejected or unknown, not %', p_outcome;
    end if;
    if p_outcome = 'accepted' and p_paypal_payout_batch_id is null then
        raise exception 'an accepted submission needs the PayPal payout_batch_id';
    end if;

    select batch_id into v_batch_id from payout_submissions where id = p_submission_id;
    if not found then
        raise exception 'submission % not found', p_submission_id;
    end if;
    select * into v_batch from batches where id = v_batch_id for update;
    select * into v_submission from payout_submissions where id = p_submission_id for update;
    if v_submission.outcome <> 'in_flight' then
        raise exception 'submission attempt % is already recorded as %', v_submission.attempt_no, v_submission.outcome;
    end if;

    update payout_submissions
       set outcome = p_outcome, http_status = p_http_status, paypal_debug_id = p_paypal_debug_id,
           paypal_payout_batch_id = p_paypal_payout_batch_id, request_body = p_request_body,
           response_body = p_response_body, error = p_error, finished_at = now()
     where id = p_submission_id;

    if p_outcome = 'accepted' then
        update batches
           set status = 'submitted', paypal_payout_batch_id = p_paypal_payout_batch_id,
               paypal_batch_status = p_paypal_batch_status
         where id = v_batch_id
        returning * into v_batch;
        -- Placeholder until PayPal reports per-item status. payout_status_at
        -- stays null so the first real event always applies.
        update batch_items set payout_status = 'PENDING', payout_status_source = 'submit'
         where batch_id = v_batch_id and committed_at is not null and payout_status is null;
    elsif p_outcome = 'rejected' then
        update batches
           set status = 'rejected', paypal_batch_status = coalesce(p_paypal_batch_status, paypal_batch_status)
         where id = v_batch_id
        returning * into v_batch;
    end if;
    return v_batch;
end $$;

-- Applies item statuses from a webhook (one item) or a poll (many).
-- p_updates: [{item_id, status, event_at, paypal_payout_item_id,
-- paypal_transaction_id, error, fee}]. An update older than what is stored,
-- or for an item that is not part of a payout, is skipped. Returns how many
-- items changed.
create function apply_item_statuses(p_updates jsonb, p_source text) returns integer
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_applied integer;
begin
    if p_source not in ('webhook', 'poll') then
        raise exception 'source must be webhook or poll, not %', p_source;
    end if;
    update batch_items i
       set payout_status = u.status,
           payout_status_at = u.event_at,
           payout_status_source = p_source,
           paypal_payout_item_id = coalesce(u.paypal_payout_item_id, i.paypal_payout_item_id),
           paypal_transaction_id = coalesce(u.paypal_transaction_id, i.paypal_transaction_id),
           payout_error = u.error,
           payout_fee = coalesce(u.fee, i.payout_fee)
      from jsonb_to_recordset(p_updates) as u(
               item_id uuid, status text, event_at timestamptz, paypal_payout_item_id text,
               paypal_transaction_id text, error jsonb, fee numeric)
     where i.id = u.item_id
       and u.status is not null and u.event_at is not null
       and i.committed_at is not null
       and (i.payout_status_at is null or i.payout_status_at <= u.event_at)
       and (i.payout_status, i.payout_status_at) is distinct from (u.status, u.event_at);
    get diagnostics v_applied = row_count;
    return v_applied;
end $$;

-- Maps a PayPal batch_status onto the batch. Never moves a batch backwards,
-- so a late PENDING after PROCESSING is recorded but changes nothing.
create function apply_batch_status(
    p_paypal_payout_batch_id text,
    p_paypal_status          text
) returns batches
language plpgsql
set search_path = public, pg_temp
as $$
declare
    v_batch  batches%rowtype;
    v_target text;
begin
    select * into v_batch from batches where paypal_payout_batch_id = p_paypal_payout_batch_id for update;
    if not found then
        return null;
    end if;
    v_target := case upper(p_paypal_status)
                    when 'PENDING' then 'submitted'
                    when 'PROCESSING' then 'processing'
                    when 'SUCCESS' then 'completed'
                    when 'DENIED' then 'rejected'
                end;
    if v_target is not null and v_target <> v_batch.status and batch_transition_allowed(v_batch.status, v_target) then
        update batches set status = v_target, paypal_batch_status = p_paypal_status, last_polled_at = now()
         where id = v_batch.id returning * into v_batch;
    else
        update batches set paypal_batch_status = p_paypal_status, last_polled_at = now()
         where id = v_batch.id returning * into v_batch;
    end if;
    return v_batch;
end $$;

-- ---------------------------------------------------------------------
-- Read models for the review grid and the reconciliation report
-- ---------------------------------------------------------------------
create view batch_item_overview with (security_invoker = true) as
select
    i.id,
    i.batch_id,
    i.row_number,
    i.recipient_id,
    i.receiver_type,
    i.receiver,
    i.recipient_name,
    i.amount,
    i.currency,
    i.invoice_id,
    i.note,
    i.decision,
    i.decision_reason,
    i.decided_by,
    i.decided_at,
    i.committed_at,
    i.paypal_payout_item_id,
    i.paypal_transaction_id,
    i.payout_status,
    i.payout_status_at,
    i.payout_error,
    b.status                           as batch_status,
    r.vetting_status                   as recipient_vetting_status,
    coalesce(f.flag_codes, '{}')       as flag_codes,
    f.top_severity,
    case
        when b.status in ('draft', 'in_review', 'approved', 'cancelled') then null
        when i.payout_status = 'SUCCESS' then 'paid'
        when is_failed_payout_status(i.payout_status) then 'failed'
        when i.decision = 'include' and b.status = 'rejected' then 'failed'
        when i.decision = 'include' then 'pending'
        when f.flag_codes is not null then 'flagged'
        else 'excluded'
    end                                as reconciliation_bucket
from batch_items i
join batches b on b.id = i.batch_id
left join recipients r on r.id = i.recipient_id
left join lateral (
    select array_agg(fl.code order by fl.code) as flag_codes,
           case max(case fl.severity when 'blocking' then 3 when 'warning' then 2 else 1 end)
               when 3 then 'blocking' when 2 then 'warning' when 1 then 'info'
           end as top_severity
      from flags fl
     where fl.batch_item_id = i.id
) f on true;

create view batch_overview with (security_invoker = true) as
select
    b.id                                                                        as batch_id,
    b.batch_no,
    b.name,
    b.status,
    b.currency,
    b.sender_batch_id,
    b.paypal_payout_batch_id,
    b.paypal_batch_status,
    b.created_at,
    b.submitted_at,
    b.completed_at,
    count(o.id)                                                                 as row_count,
    count(o.id) filter (where o.decision = 'include')                           as included_count,
    coalesce(sum(o.amount) filter (where o.decision = 'include'), 0)            as included_total,
    count(o.id) filter (where o.decision = 'exclude')                           as excluded_count,
    count(o.id) filter (where o.decision = 'pending')                           as undecided_count,
    count(o.id) filter (where o.top_severity = 'blocking')                      as blocking_rows,
    count(o.id) filter (where o.top_severity = 'warning')                       as warning_rows,
    count(o.id) filter (where o.reconciliation_bucket = 'paid')                 as paid_count,
    coalesce(sum(o.amount) filter (where o.reconciliation_bucket = 'paid'), 0)  as paid_total,
    count(o.id) filter (where o.reconciliation_bucket = 'pending')              as payout_pending_count,
    coalesce(sum(o.amount) filter (where o.reconciliation_bucket = 'pending'), 0) as payout_pending_total,
    count(o.id) filter (where o.reconciliation_bucket = 'failed')               as failed_count,
    coalesce(sum(o.amount) filter (where o.reconciliation_bucket = 'failed'), 0) as failed_total,
    count(o.id) filter (where o.reconciliation_bucket = 'flagged')              as flagged_count
from batches b
left join batch_item_overview o on o.batch_id = b.id
group by b.id;

-- ---------------------------------------------------------------------
-- Access: the FastAPI backend uses the service-role key. The browser's
-- anon key gets nothing: RLS is on with no policies, and grants are revoked.
-- ---------------------------------------------------------------------
alter table app_users          enable row level security;
alter table recipients         enable row level security;
alter table batches            enable row level security;
alter table batch_items        enable row level security;
alter table flags              enable row level security;
alter table approvals          enable row level security;
alter table payout_submissions enable row level security;
alter table webhook_events     enable row level security;
alter table audit_log          enable row level security;

revoke execute on all functions in schema public from public;

do $$
begin
    if exists (select 1 from pg_roles where rolname = 'anon') then
        revoke all on all tables in schema public from anon, authenticated;
        revoke all on all sequences in schema public from anon, authenticated;
        revoke execute on all functions in schema public from anon, authenticated;
    end if;
    if exists (select 1 from pg_roles where rolname = 'service_role') then
        grant execute on all functions in schema public to service_role;
    end if;
end $$;

-- ---------------------------------------------------------------------
-- Demo users (emails match the defaults in backend/.env.example)
-- ---------------------------------------------------------------------
insert into app_users (email, display_name, role) values
    ('admin@paypap.demo', 'Demo Admin', 'admin'),
    ('approver@paypap.demo', 'Demo Approver', 'approver')
on conflict (lower(email)) do nothing;

commit;
