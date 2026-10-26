-- Exercises the schema's guarantees end to end. Everything runs in one
-- transaction that is rolled back, so it leaves no data behind:
--   psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f backend/db/tests/test_schema.sql
-- Prints "schema tests passed" on success; the first failed check aborts.

\set ON_ERROR_STOP 1
begin;

create function pg_temp.expect_error(p_sql text, p_fragment text) returns void
language plpgsql
as $$
begin
    begin
        execute p_sql;
    exception when others then
        if position(p_fragment in sqlerrm) = 0 then
            raise exception 'expected an error containing "%", got: %', p_fragment, sqlerrm;
        end if;
        return;
    end;
    raise exception 'expected an error containing "%", but the statement succeeded: %', p_fragment, p_sql;
end $$;

do $$
declare
    v_admin     uuid := (select id from app_users where role = 'admin' limit 1);
    v_approver  uuid := (select id from app_users where role = 'approver' limit 1);
    v_batch     uuid;
    v_batch2    uuid;
    v_batch3    uuid;
    v_sender    text;
    v_approval  approvals%rowtype;
    v_sub       payout_submissions%rowtype;
    v_sub2      payout_submissions%rowtype;
    v_b         batches%rowtype;
    v_n         integer;
    v_ctx       jsonb;
    v_item2     uuid;
    v_item3     uuid;
    v_item5     uuid;
    v_ov        batch_overview%rowtype;
    v_chain     record;
    v_sig       text := repeat('a', 64);
    v_exp       timestamptz := now() + interval '1 hour';
begin
    -- Import ---------------------------------------------------------------
    v_batch := import_batch(
        v_admin,
        '{"name": "Test batch", "currency": "USD", "rules_version": "test"}',
        '[{"receiver_type": "EMAIL", "receiver": "ann@example.com", "full_name": "Ann"},
          {"receiver_type": "EMAIL", "receiver": "bob@example.com", "full_name": "Bob"},
          {"receiver_type": "EMAIL", "receiver": "cara@example.com", "full_name": "Cara"}]',
        '[{"row_number": 2, "raw": {}, "receiver": "ann@example.com", "amount": 100, "currency": "USD", "invoice_id": "INV-1", "decision": "include"},
          {"row_number": 3, "raw": {}, "receiver": "bob@example.com", "amount": 50, "currency": "USD", "invoice_id": "INV-2", "decision": "include"},
          {"row_number": 4, "raw": {}, "receiver": null, "amount": 20, "currency": "USD", "invoice_id": "INV-9", "decision": "exclude"},
          {"row_number": 5, "raw": {}, "receiver": "cara@example.com", "amount": 30, "currency": "USD", "invoice_id": "INV-3"},
          {"row_number": 6, "raw": {}, "receiver": "ann@example.com", "amount": 100, "currency": "USD", "invoice_id": "inv-1"}]',
        '[{"row_number": 4, "code": "MISSING_RECEIVER", "severity": "blocking", "message": "no email"},
          {"row_number": 5, "code": "NEW_RECIPIENT", "severity": "warning", "message": "new"},
          {"row_number": 6, "code": "DUPLICATE_INVOICE", "severity": "warning", "message": "dup"}]');

    select * into v_b from batches where id = v_batch;
    assert v_b.status = 'in_review', 'import leaves the batch in_review';
    assert (select count(*) from batch_items where batch_id = v_batch) = 5, 'five rows imported';
    assert (select count(*) from flags where batch_id = v_batch) = 3, 'three flags';
    assert (select count(*) from recipients) = 3, 'three recipients created';
    assert (select decision from batch_items where batch_id = v_batch and row_number = 5) = 'pending', 'warned row stays pending';
    assert (select recipient_id is not null from batch_items where batch_id = v_batch and row_number = 2), 'row linked to recipient';
    v_sender := v_b.sender_batch_id;
    select id into v_item2 from batch_items where batch_id = v_batch and row_number = 2;
    select id into v_item3 from batch_items where batch_id = v_batch and row_number = 3;
    select id into v_item5 from batch_items where batch_id = v_batch and row_number = 5;

    perform pg_temp.expect_error(format(
        $q$select import_batch(%L, '{"name":"x","rules_version":"t"}', '[]', '[{"row_number":2,"raw":{}}]', '[{"row_number":9,"code":"X","severity":"info","message":"m"}]')$q$,
        v_admin), 'flags refer to rows that are not in the batch');
    perform pg_temp.expect_error(
        $q$insert into recipients (receiver, full_name) values ('Mixed@Example.com', 'M')$q$,
        'recipients_email_lowercase');

    -- Decisions ------------------------------------------------------------
    perform pg_temp.expect_error(format(
        $q$select set_item_decisions(%L, %L, '[{"row_number": 4, "decision": "include"}]')$q$, v_batch, v_admin),
        'blocking flag');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 3, 180, %L, %L)$q$, v_batch, v_approver, v_exp, v_sig),
        'still need an include or exclude decision');

    v_n := set_item_decisions(v_batch, v_admin,
        '[{"row_number": 5, "decision": "include"}, {"row_number": 6, "decision": "include", "reason": "checked"}]');
    assert v_n = 2, 'two decisions changed';
    assert (select decided_by from batch_items where id = v_item5) = v_admin, 'decided_by recorded';

    -- Approval gate --------------------------------------------------------
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 4, 280, %L, %L)$q$, v_batch, v_approver, v_exp, v_sig),
        'would be paid twice');
    perform set_item_decisions(v_batch, v_admin, '[{"row_number": 6, "decision": "exclude", "reason": "duplicate"}]');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 3, 180, %L, %L)$q$, v_batch, v_admin, v_exp, v_sig),
        'is not an approver');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 3, 999, %L, %L)$q$, v_batch, v_approver, v_exp, v_sig),
        'batch changed since it was reviewed');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 3, 180, now() + interval '2 days', %L)$q$, v_batch, v_approver, v_sig),
        'within the next 24 hours');
    perform pg_temp.expect_error(format(
        $q$update batches set status = 'approved' where id = %L$q$, v_batch),
        'only become approved through approve_batch()');

    v_approval := approve_batch(gen_random_uuid(), v_batch, v_approver, 3, 180.00, v_exp, v_sig, 'looks right');
    assert v_approval.item_count = 3 and v_approval.total_amount = 180 and v_approval.warned_count = 1, 'approval snapshot';
    assert (select status from batches where id = v_batch) = 'approved', 'batch approved';
    assert (select count(*) from batch_items where batch_id = v_batch and committed_at is not null) = 3, 'three rows locked';

    -- Locks and immutability -----------------------------------------------
    perform pg_temp.expect_error(format($q$update batch_items set amount = 999 where id = %L$q$, v_item2), 'locked by an approval');
    perform pg_temp.expect_error(format($q$update batches set sender_batch_id = 'other' where id = %L$q$, v_batch), 'sender_batch_id is fixed');
    perform pg_temp.expect_error(format($q$update approvals set total_amount = 1 where id = %L$q$, v_approval.id), 'approval is immutable');
    perform pg_temp.expect_error(format($q$delete from approvals where id = %L$q$, v_approval.id), 'cannot be deleted');
    perform pg_temp.expect_error(format($q$update batches set status = 'submitting' where id = %L$q$, v_batch), 'claim_batch_for_submit()');
    perform pg_temp.expect_error(format($q$update batches set status = 'completed' where id = %L$q$, v_batch), 'cannot move from approved to completed');
    perform pg_temp.expect_error(format(
        $q$insert into batch_items (batch_id, row_number, raw) values (%L, 99, '{}')$q$, v_batch),
        'only be added to a draft batch');

    -- Submission and retry -------------------------------------------------
    v_sub := claim_batch_for_submit(v_batch, v_approval.id, v_admin);
    assert v_sub.attempt_no = 1 and v_sub.sender_batch_id = v_sender, 'first attempt uses the batch sender_batch_id';
    assert (select status from batches where id = v_batch) = 'submitting', 'batch submitting';
    perform pg_temp.expect_error(format($q$select claim_batch_for_submit(%L, %L)$q$, v_batch, v_approval.id), 'already in progress');

    perform record_submission_result(v_sub.id, 'unknown', p_error => 'timeout');
    v_sub2 := claim_batch_for_submit(v_batch, v_approval.id);
    assert v_sub2.attempt_no = 2 and v_sub2.sender_batch_id = v_sender, 'retry reuses sender_batch_id';
    perform pg_temp.expect_error(format($q$select record_submission_result(%L, 'accepted')$q$, v_sub2.id), 'needs the PayPal payout_batch_id');

    v_b := record_submission_result(v_sub2.id, 'accepted', 201, 'dbg', 'PB-1', 'PENDING');
    assert v_b.status = 'submitted' and v_b.paypal_payout_batch_id = 'PB-1' and v_b.submitted_at is not null, 'batch submitted';
    assert (select count(*) from batch_items where batch_id = v_batch and payout_status = 'PENDING') = 3, 'items pending';
    perform pg_temp.expect_error(format($q$select claim_batch_for_submit(%L, %L)$q$, v_batch, v_approval.id), 'only an approved batch');
    perform pg_temp.expect_error(format($q$select record_submission_result(%L, 'rejected')$q$, v_sub2.id), 'already recorded');

    -- Status updates, out of order ------------------------------------------
    v_n := apply_item_statuses(jsonb_build_array(
        jsonb_build_object('item_id', v_item2, 'status', 'SUCCESS', 'event_at', '2026-10-24T10:05:00Z', 'paypal_transaction_id', 'TX2'),
        jsonb_build_object('item_id', v_item3, 'status', 'FAILED', 'event_at', '2026-10-24T10:05:00Z', 'error', '{"name": "RECEIVER_UNREGISTERED"}'),
        jsonb_build_object('item_id', v_item5, 'status', 'UNCLAIMED', 'event_at', '2026-10-24T10:04:00Z')), 'poll');
    assert v_n = 3, 'three statuses applied';
    v_n := apply_item_statuses(jsonb_build_array(
        jsonb_build_object('item_id', v_item2, 'status', 'PENDING', 'event_at', '2026-10-24T10:01:00Z')), 'webhook');
    assert v_n = 0, 'older event ignored';
    v_n := apply_item_statuses(jsonb_build_array(
        jsonb_build_object('item_id', v_item2, 'status', 'SUCCESS', 'event_at', '2026-10-24T10:05:00Z')), 'webhook');
    assert v_n = 0, 'replayed event is a no-op';
    assert (select payout_status from batch_items where id = v_item2) = 'SUCCESS', 'status kept';

    v_b := apply_batch_status('PB-1', 'PROCESSING');
    assert v_b.status = 'processing', 'processing';
    v_b := apply_batch_status('PB-1', 'PENDING');
    assert v_b.status = 'processing' and v_b.paypal_batch_status = 'PENDING', 'late PENDING does not move the batch back';
    v_b := apply_batch_status('PB-1', 'SUCCESS');
    assert v_b.status = 'completed' and v_b.completed_at is not null, 'completed';
    assert apply_batch_status('unknown', 'SUCCESS') is null, 'unknown payout batch ignored';

    select * into v_ov from batch_overview where batch_id = v_batch;
    assert v_ov.paid_count = 1 and v_ov.paid_total = 100, 'paid bucket';
    assert v_ov.failed_count = 1 and v_ov.failed_total = 50, 'failed bucket';
    assert v_ov.payout_pending_count = 1, 'pending bucket';
    assert v_ov.flagged_count = 2, 'flagged bucket (excluded rows with flags)';

    -- Paying an invoice twice across batches ---------------------------------
    v_ctx := import_context('EMAIL', array['ann@example.com', 'bob@example.com']);
    assert jsonb_array_length(v_ctx -> 'recipients') = 2, 'context recipients';
    assert jsonb_array_length(v_ctx -> 'paid_invoices') = 1, 'only the successful invoice counts as paid (failed one can be retried)';

    v_batch2 := import_batch(v_admin, '{"name": "Second", "rules_version": "test"}', '[]',
        '[{"row_number": 2, "raw": {}, "receiver": "ann@example.com", "amount": 100, "currency": "USD", "invoice_id": "INV-1", "decision": "include"},
          {"row_number": 3, "raw": {}, "receiver": "bob@example.com", "amount": 50, "currency": "USD", "invoice_id": "INV-2", "decision": "include"}]',
        '[]');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 2, 150, %L, %L)$q$, v_batch2, v_approver, v_exp, v_sig),
        'INV-1');
    perform set_item_decisions(v_batch2, v_admin, '[{"row_number": 2, "decision": "exclude", "reason": "already paid"}]');
    v_approval := approve_batch(gen_random_uuid(), v_batch2, v_approver, 1, 50, v_exp, v_sig);
    assert v_approval.item_count = 1, 'failed invoice can be paid again in a new batch';

    -- The unique index is the backstop even if the function checks were skipped.
    perform pg_temp.expect_error(format($q$
        update batch_items set payout_status = 'SUCCESS', payout_status_at = now() where id = %L$q$, v_item3),
        'batch_items_invoice_paid_once');

    -- Revoke releases the locks ------------------------------------------------
    v_b := revoke_approval(v_approval.id, 'changed my mind', v_approver);
    assert v_b.status = 'in_review', 'back to review';
    assert (select count(*) from batch_items where batch_id = v_batch2 and committed_at is not null) = 0, 'locks released';
    perform pg_temp.expect_error(format($q$select revoke_approval(%L, 'again')$q$, v_approval.id), 'already revoked');

    -- Uploader cannot approve their own batch -----------------------------------
    v_batch3 := import_batch(v_approver, '{"name": "Self", "rules_version": "test"}', '[]',
        '[{"row_number": 2, "raw": {}, "receiver": "cara@example.com", "amount": 10, "currency": "USD", "invoice_id": "INV-7", "decision": "include"}]',
        '[]');
    perform pg_temp.expect_error(format(
        $q$select approve_batch(gen_random_uuid(), %L, %L, 1, 10, %L, %L)$q$, v_batch3, v_approver, v_exp, v_sig),
        'cannot approve it');

    -- Rejected submission releases locks -------------------------------------
    v_approval := approve_batch(gen_random_uuid(), v_batch2, v_approver, 1, 50, v_exp, v_sig);
    v_sub := claim_batch_for_submit(v_batch2, v_approval.id);
    v_b := record_submission_result(v_sub.id, 'rejected', 422, p_error => 'VALIDATION_ERROR');
    assert v_b.status = 'rejected', 'rejected';
    assert (select count(*) from batch_items where batch_id = v_batch2 and committed_at is not null) = 0, 'rejected batch releases locks';

    -- Webhook idempotency ----------------------------------------------------
    insert into webhook_events (paypal_event_id, event_type, payload, payout_batch_id)
    values ('WH-1', 'PAYMENT.PAYOUTS-ITEM.SUCCEEDED', '{}', 'PB-1') on conflict (paypal_event_id) do nothing;
    insert into webhook_events (paypal_event_id, event_type, payload, payout_batch_id)
    values ('WH-1', 'PAYMENT.PAYOUTS-ITEM.SUCCEEDED', '{}', 'PB-1') on conflict (paypal_event_id) do nothing;
    assert (select count(*) from webhook_events) = 1, 'redelivered webhook stored once';

    -- Audit log ----------------------------------------------------------------
    assert (select count(*) from audit_log where batch_id = v_batch and action = 'batch.approved') = 1, 'approval audited';
    assert (select count(*) from audit_log where batch_id = v_batch and action = 'flag.raised') = 3, 'flags audited';
    assert (select count(*) from audit_log where batch_id = v_batch and action = 'item.decision_changed'
              and actor_type = 'user' and actor_id = v_admin) >= 3, 'human decisions audited with actor';
    assert (select count(*) from audit_log where batch_id = v_batch and action = 'item.payout_status_changed') = 6, 'status changes audited';
    assert (select count(*) from audit_log where batch_id = v_batch and action like 'payout.submit_%') = 4, 'submission attempts audited';
    perform pg_temp.expect_error('update audit_log set action = ''x.y'' where id = 1', 'append-only');
    perform pg_temp.expect_error('delete from audit_log where id = 1', 'append-only');

    select * into v_chain from verify_audit_chain();
    assert v_chain.first_bad_id is null and v_chain.rows_checked = (select count(*) from audit_log), 'audit chain intact';

    -- Tampering (possible only by disabling the trigger) is detected.
    alter table audit_log disable trigger audit_log_no_update;
    update audit_log set details = '{"tampered": true}' where id = 5;
    alter table audit_log enable trigger audit_log_no_update;
    select * into v_chain from verify_audit_chain();
    assert v_chain.first_bad_id = 5 and v_chain.rows_checked = 4, 'tampered row found';
end $$;

-- The browser's anon role can read nothing.
set local role anon;
select pg_temp.expect_error('select * from batches', 'permission denied');
select pg_temp.expect_error('select verify_audit_chain()', 'permission denied');
reset role;

-- The backend's service role can call the functions.
set local role service_role;
select count(*) as service_role_can_read from batch_overview;
reset role;

select 'schema tests passed' as result;
rollback;
