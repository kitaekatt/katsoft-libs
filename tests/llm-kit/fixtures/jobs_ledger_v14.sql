BEGIN TRANSACTION;
CREATE TABLE attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            status TEXT NOT NULL,
            error_code TEXT,
            error_message TEXT,
            halt_kind TEXT,
            dropped_params_json TEXT,
            execution_controls_applied_json TEXT,
            started_at TEXT,
            ended_at TEXT,
            input_tokens INTEGER,
            output_tokens INTEGER,
            cache_hit_tokens INTEGER,
            total_tokens INTEGER,
            response_text TEXT,
            workspace TEXT,
            acceptance_json TEXT, base_ref TEXT, workspace_status TEXT NOT NULL DEFAULT 'none', workspace_reason TEXT, workspace_removed_at REAL, workspace_removal_forced INTEGER NOT NULL DEFAULT 0, forwarded_params_json TEXT, reasoning TEXT, finish_reason TEXT, pace_readings_json TEXT, session_mode TEXT NOT NULL DEFAULT 'completion', session_id TEXT, session_cwd TEXT, session_dir TEXT, stop_kind TEXT,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id)
        );
INSERT INTO "attempts" VALUES(1,'run','isolated',1,'fake-endpoint','fake-backend','fake-model','completed',NULL,NULL,NULL,NULL,NULL,'2026-11-01T00:00:01Z','2026-11-01T00:00:02Z',NULL,NULL,NULL,NULL,'answer','/fixture/workspaces/isolated/attempt-1',NULL,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','isolated','Git detached worktree',NULL,0,NULL,NULL,NULL,NULL,'completion',NULL,NULL,NULL,NULL);
CREATE TABLE continuations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            continuation_no INTEGER NOT NULL,
            interrupt_id INTEGER NOT NULL REFERENCES interrupts(id),
            started_at REAL NOT NULL,
            ended_at REAL,
            disposition TEXT,
            acceptance_json TEXT,
            FOREIGN KEY (run_id, job_id, attempt_no)
                REFERENCES attempts(run_id, job_id, attempt_no),
            UNIQUE (run_id, job_id, attempt_no, continuation_no)
        );
CREATE TABLE events (
            seq INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL REFERENCES runs(id),
            job_id TEXT,
            attempt_no INTEGER,
            event TEXT NOT NULL,
            at TEXT NOT NULL,
            adapter TEXT,
            model TEXT,
            payload_json TEXT NOT NULL
        , schema TEXT);
INSERT INTO "events" VALUES(1,'run',NULL,NULL,'job-kit:run-created','2026-11-14T20:15:07.373223Z',NULL,NULL,'{"job_count": 3, "max_parallel": 1}','plugins-kit.execution-event/v1');
INSERT INTO "events" VALUES(2,'run','isolated',1,'dispatch-selected','2026-11-01T00:00:00Z','fake-backend','fake-model','{"budget_no": 1, "endpoint": "fake-endpoint"}','plugins-kit.execution-event/v1');
INSERT INTO "events" VALUES(3,'run','isolated',1,'call-started','2026-11-01T00:00:01Z','fake-backend','fake-model','{}','plugins-kit.execution-event/v1');
INSERT INTO "events" VALUES(4,'run','isolated',1,'result','2026-11-01T00:00:02Z','fake-backend','fake-model','{"status": "completed"}','plugins-kit.execution-event/v1');
INSERT INTO "events" VALUES(5,'run','agent',1,'dispatch-selected','2026-11-01T00:00:00Z','fake-backend','fake-model','{"budget_no": 1, "endpoint": "fake-endpoint"}','plugins-kit.execution-event/v1');
INSERT INTO "events" VALUES(6,'run','live',1,'dispatch-selected','2026-11-01T00:00:00Z','fake-backend','fake-model','{"budget_no": 1, "endpoint": "fake-endpoint"}','plugins-kit.execution-event/v1');
CREATE TABLE "interrupt_resolutions" (
            interrupt_id INTEGER PRIMARY KEY REFERENCES interrupts(id),
            outcome TEXT NOT NULL
                CHECK (outcome IN ('answered', 'rejected', 'expired', 'cancelled')),
            input_json TEXT,
            reason TEXT,
            resolved_at REAL NOT NULL
        );
CREATE TABLE interrupts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            continuation_no INTEGER NOT NULL,
            envelope TEXT NOT NULL,
            kind TEXT NOT NULL,
            request_schema_json TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            created_at REAL NOT NULL,
            expires_at REAL,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id),
            FOREIGN KEY (run_id, job_id, attempt_no)
                REFERENCES attempts(run_id, job_id, attempt_no),
            UNIQUE (run_id, job_id, attempt_no, continuation_no)
        );
CREATE TABLE jobs (
            run_id TEXT NOT NULL REFERENCES runs(id),
            id TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            definition_json TEXT NOT NULL,
            state TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL, error_message TEXT,
            PRIMARY KEY (run_id, id)
        );
INSERT INTO "jobs" VALUES('run','isolated',0,'{"contract": {"command": ["true"], "directory": "/fixture/repo"}, "directory": "/fixture/repo", "id": "isolated", "max_attempts": 2, "models": ["fake"], "options": {}, "prompt": {"system": "", "user": "run isolated"}, "requirements": {}, "workspace": {"directory": "/fixture/repo", "isolate": true}}','pending',1.794687307373222828e+09,1794687307.37999,NULL);
INSERT INTO "jobs" VALUES('run','agent',1,'{"contract": {"command": ["true"], "directory": "/fixture/repo"}, "directory": "/fixture/repo", "id": "agent", "max_attempts": 2, "models": ["fake"], "options": {}, "prompt": {"system": "", "user": "run agent"}, "requirements": {}, "workspace": {"directory": "/fixture/repo", "isolate": true}}','running',1.794687307373222828e+09,1.794687307381592036e+09,NULL);
INSERT INTO "jobs" VALUES('run','live',2,'{"contract": {"command": ["true"], "directory": "/fixture/repo"}, "directory": "/fixture/repo", "id": "live", "max_attempts": 2, "models": ["fake"], "options": {}, "prompt": {"system": "", "user": "run live"}, "requirements": {}, "workspace": {"directory": "/fixture/repo", "isolate": true}}','running',1.794687307373222828e+09,1.794687307383250951e+09,NULL);
CREATE TABLE reservations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT NOT NULL,
            job_id TEXT NOT NULL,
            attempt_no INTEGER NOT NULL,
            budget_no INTEGER NOT NULL,
            endpoint TEXT NOT NULL,
            backend TEXT NOT NULL,
            model TEXT NOT NULL,
            workspace_path TEXT,
            reserved_at TEXT NOT NULL,
            invoke_armed_at TEXT,
            disposition TEXT,
            resolved_at TEXT,
            lost_at TEXT,
            loss_reason TEXT,
            workspace_status TEXT NOT NULL DEFAULT 'none',
            workspace_reason TEXT,
            workspace_removed_at REAL,
            workspace_removal_forced INTEGER NOT NULL DEFAULT 0, session_mode TEXT NOT NULL DEFAULT 'completion', session_id TEXT, session_cwd TEXT, session_dir TEXT, stop_kind TEXT,
            FOREIGN KEY (run_id, job_id) REFERENCES jobs(run_id, id),
            UNIQUE (run_id, job_id, attempt_no)
        );
INSERT INTO "reservations" VALUES(1,'run','isolated',1,1,'fake-endpoint','fake-backend','fake-model','/fixture/workspaces/isolated/attempt-1','2026-11-01T00:00:00Z','2026-11-01T00:00:01Z','completed','1794687307.37999',NULL,NULL,'isolated',NULL,NULL,0,'completion',NULL,NULL,NULL,NULL);
INSERT INTO "reservations" VALUES(2,'run','agent',1,1,'fake-endpoint','fake-backend','fake-model','/fixture/workspaces/agent/attempt-1','2026-11-01T00:00:00Z',NULL,NULL,NULL,NULL,NULL,'isolated',NULL,NULL,0,'agent','11111111-2222-4333-8444-555555555555','/fixture/workspaces/agent/attempt-1','/fixture/sessions/agent/attempt-1',NULL);
INSERT INTO "reservations" VALUES(3,'run','live',1,1,'fake-endpoint','fake-backend','fake-model',NULL,'2026-11-01T00:00:00Z',NULL,NULL,NULL,NULL,NULL,'none',NULL,NULL,0,'completion',NULL,NULL,NULL,NULL);
CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            created_at REAL NOT NULL,
            jobs_path TEXT,
            max_parallel INTEGER NOT NULL,
            workspace_root TEXT
        , workspace_base_refs_json TEXT, disallowed_tools TEXT, events_recorded INTEGER NOT NULL DEFAULT 0);
INSERT INTO "runs" VALUES('run',1.794687307373222828e+09,NULL,1,'/fixture/workspaces','{"agent": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "isolated": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "live": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}',NULL,1);
CREATE TABLE schema_version (
            version INTEGER NOT NULL
        );
INSERT INTO "schema_version" VALUES(14);
CREATE INDEX idx_jobs_run_state ON jobs(run_id, state);
CREATE INDEX idx_attempts_run_job ON attempts(run_id, job_id, id);
CREATE INDEX idx_reservations_run_job ON reservations(run_id, job_id, id);
CREATE UNIQUE INDEX idx_attempts_run_job_no ON attempts(run_id, job_id, attempt_no);
CREATE INDEX idx_events_run_seq ON events(run_id, seq);
CREATE INDEX idx_interrupts_run_job ON interrupts(run_id, job_id, id);
CREATE INDEX idx_continuations_run_job ON continuations(run_id, job_id, id);
CREATE TRIGGER interrupts_immutable_update BEFORE UPDATE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END;
CREATE TRIGGER interrupts_immutable_delete BEFORE DELETE ON interrupts
            BEGIN SELECT RAISE(ABORT, 'interrupt records are immutable'); END;
CREATE TRIGGER interrupts_one_open_per_job BEFORE INSERT ON interrupts
            WHEN EXISTS (
                SELECT 1 FROM interrupts AS open
                WHERE open.run_id = NEW.run_id AND open.job_id = NEW.job_id
                  AND NOT EXISTS (
                      SELECT 1 FROM interrupt_resolutions AS r
                      WHERE r.interrupt_id = open.id))
            BEGIN SELECT RAISE(ABORT, 'job already has an unresolved interrupt'); END;
CREATE TRIGGER resolutions_immutable_update BEFORE UPDATE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END;
CREATE TRIGGER resolutions_immutable_delete BEFORE DELETE ON interrupt_resolutions
            BEGIN SELECT RAISE(ABORT, 'interrupt resolutions are immutable'); END;
DELETE FROM "sqlite_sequence";
INSERT INTO "sqlite_sequence" VALUES('events',6);
INSERT INTO "sqlite_sequence" VALUES('reservations',3);
INSERT INTO "sqlite_sequence" VALUES('attempts',1);
COMMIT;
