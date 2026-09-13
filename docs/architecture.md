# Architecture

The frozen design is one FastAPI/Uvicorn process, one SQLite database, one durable work loop, one structured semantic reasoner, deterministic policy/execution code, and three direct provider adapters. Evidence and assessments are immutable snapshots. Actions are reserved before network calls, and every acknowledged write is read back before it is shown as verified.

Workflow progress (`QUEUED`, `RUNNING`, `AWAITING_APPROVAL`, `IDLE`, `RECOVERING`, `NEEDS_OPERATOR`) is separate from business readiness (`UNKNOWN`, `BLOCKED`, `READY_FOR_PAYMENT`, `CLOSED`). A successful Jira issue or Gmail draft cannot make an invoice ready.

The canonical design review is the separate `ClearDue-Engineering-Blueprint.md` deliverable in the parent workspace outputs directory.

