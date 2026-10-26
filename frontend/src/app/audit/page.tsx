"use client";

import AuditTable from "@/components/AuditTable";
import { PageTitle } from "@/components/Status";
import { useUser } from "@/lib/useSession";

export default function AuditPage() {
  const user = useUser();
  return (
    <section className="glass-panel" style={{ padding: 18, display: "flex", flexDirection: "column", gap: 16 }}>
      <PageTitle
        title="Audit log"
        sub="Every flag, decision, approval, submission and PayPal status change, newest first. Entries cannot be edited or deleted, and each is hash-chained to the one before."
      />
      {user ? <AuditTable /> : null}
    </section>
  );
}
