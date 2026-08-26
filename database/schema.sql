-- StoryWeaver PostgreSQL 初始 Schema
-- 由当前 SQLAlchemy ORM 元数据生成；用于空数据库初始化。
-- 脚本可重复执行，但不会将已有数据库升级到新结构。


CREATE TABLE IF NOT EXISTS books (
	book_id VARCHAR(80) NOT NULL, 
	metadata_json JSONB NOT NULL, 
	foundation_json JSONB NOT NULL, 
	initial_state_json JSONB NOT NULL, 
	state_json JSONB NOT NULL, 
	creative_control_json JSONB NOT NULL, 
	version BIGINT NOT NULL, 
	PRIMARY KEY (book_id)
)

;


CREATE TABLE IF NOT EXISTS chat_sessions (
	session_id VARCHAR(64) NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	title VARCHAR(200) NOT NULL, 
	book_id VARCHAR(80), 
	updated_at VARCHAR(64) NOT NULL, 
	last_sequence INTEGER NOT NULL, 
	message_count INTEGER NOT NULL, 
	PRIMARY KEY (session_id)
)

;


CREATE TABLE IF NOT EXISTS jobs (
	job_id VARCHAR(64) NOT NULL, 
	job_type VARCHAR(64) NOT NULL, 
	book_id VARCHAR(80), 
	lock_scope VARCHAR(16) NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	payload_json JSONB NOT NULL, 
	result_json JSONB, 
	error TEXT, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (job_id)
)

;


CREATE TABLE IF NOT EXISTS long_term_memories (
	memory_id VARCHAR(128) NOT NULL, 
	scope_type VARCHAR(32) NOT NULL, 
	scope_id VARCHAR(128) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	fingerprint VARCHAR(128) NOT NULL, 
	record_json JSONB NOT NULL, 
	PRIMARY KEY (memory_id), 
	CONSTRAINT uq_memory_scope_fingerprint_status UNIQUE (scope_type, scope_id, fingerprint, status)
)

;


CREATE TABLE IF NOT EXISTS action_proposals (
	proposal_id VARCHAR(64) NOT NULL, 
	session_id VARCHAR(64) NOT NULL, 
	book_id VARCHAR(80), 
	action_type VARCHAR(64) NOT NULL, 
	payload_json JSONB NOT NULL, 
	summary TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	job_id VARCHAR(64), 
	created_at VARCHAR(64) NOT NULL, 
	confirmed_at VARCHAR(64), 
	expires_at VARCHAR(64), 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (proposal_id), 
	FOREIGN KEY(session_id) REFERENCES chat_sessions (session_id) ON DELETE CASCADE, 
	UNIQUE (job_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (job_id) ON DELETE SET NULL
)

;


CREATE TABLE IF NOT EXISTS chapter_runs (
	run_id VARCHAR(128) NOT NULL, 
	book_id VARCHAR(80) NOT NULL, 
	chapter_number INTEGER NOT NULL, 
	run_type VARCHAR(24) NOT NULL, 
	status VARCHAR(32) NOT NULL, 
	version INTEGER NOT NULL, 
	base_book_version BIGINT NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	plan_json JSONB, 
	plan_history_json JSONB NOT NULL, 
	artifacts_json JSONB NOT NULL, 
	PRIMARY KEY (run_id), 
	FOREIGN KEY(book_id) REFERENCES books (book_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chapters (
	id SERIAL NOT NULL, 
	book_id VARCHAR(80) NOT NULL, 
	chapter_number INTEGER NOT NULL, 
	metadata_json JSONB NOT NULL, 
	draft_json JSONB NOT NULL, 
	original_draft_json JSONB NOT NULL, 
	plan_json JSONB NOT NULL, 
	delta_json JSONB NOT NULL, 
	initial_review_json JSONB, 
	final_review_json JSONB, 
	context_trace_json JSONB, 
	draft_history_json JSONB NOT NULL, 
	review_history_json JSONB NOT NULL, 
	state_after_json JSONB NOT NULL, 
	source_run_id VARCHAR(128), 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chapter_book_number UNIQUE (book_id, chapter_number), 
	FOREIGN KEY(book_id) REFERENCES books (book_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS chat_session_events (
	id SERIAL NOT NULL, 
	session_id VARCHAR(64) NOT NULL, 
	sequence INTEGER NOT NULL, 
	event_json JSONB NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_chat_event_sequence UNIQUE (session_id, sequence), 
	FOREIGN KEY(session_id) REFERENCES chat_sessions (session_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS context_snapshots (
	snapshot_id VARCHAR(64) NOT NULL, 
	job_id VARCHAR(64), 
	simulation_id VARCHAR(64), 
	simulation_turn_id VARCHAR(64), 
	simulation_stage VARCHAR(32), 
	simulation_character_id VARCHAR(128), 
	simulation_ordinal INTEGER, 
	book_id VARCHAR(80), 
	agent_role VARCHAR(64) NOT NULL, 
	book_version BIGINT, 
	policy_version VARCHAR(64) NOT NULL, 
	renderer_version VARCHAR(64) NOT NULL, 
	rendered_context TEXT NOT NULL, 
	trace_json JSONB NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (snapshot_id), 
	FOREIGN KEY(job_id) REFERENCES jobs (job_id) ON DELETE SET NULL, 
	FOREIGN KEY(book_id) REFERENCES books (book_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS job_events (
	id SERIAL NOT NULL, 
	job_id VARCHAR(64) NOT NULL, 
	sequence INTEGER NOT NULL, 
	event_type VARCHAR(64) NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	payload_json JSONB NOT NULL, 
	PRIMARY KEY (id), 
	CONSTRAINT uq_job_event_sequence UNIQUE (job_id, sequence), 
	FOREIGN KEY(job_id) REFERENCES jobs (job_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS simulation_sessions (
	simulation_id VARCHAR(64) NOT NULL, 
	book_id VARCHAR(80) NOT NULL, 
	base_book_version BIGINT NOT NULL, 
	base_chapter_number INTEGER NOT NULL, 
	mode VARCHAR(16) NOT NULL, 
	user_character_id VARCHAR(128), 
	status VARCHAR(16) NOT NULL, 
	current_turn INTEGER NOT NULL, 
	version INTEGER NOT NULL, 
	base_snapshot_json JSONB NOT NULL, 
	scene_config_json JSONB NOT NULL, 
	current_state_json JSONB NOT NULL, 
	created_at VARCHAR(64) NOT NULL, 
	updated_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (simulation_id), 
	FOREIGN KEY(book_id) REFERENCES books (book_id) ON DELETE CASCADE
)

;


CREATE TABLE IF NOT EXISTS simulation_turns (
	turn_id VARCHAR(64) NOT NULL, 
	simulation_id VARCHAR(64) NOT NULL, 
	turn_number INTEGER NOT NULL, 
	job_id VARCHAR(64), 
	client_request_id VARCHAR(128) NOT NULL, 
	mode VARCHAR(16) NOT NULL, 
	user_character_id VARCHAR(128), 
	target_character_id VARCHAR(128), 
	input_type VARCHAR(32) NOT NULL, 
	user_input TEXT NOT NULL, 
	status VARCHAR(16) NOT NULL, 
	output_json JSONB, 
	state_delta_json JSONB, 
	state_before_hash VARCHAR(128), 
	state_after_hash VARCHAR(128), 
	context_snapshot_id VARCHAR(64), 
	created_at VARCHAR(64) NOT NULL, 
	PRIMARY KEY (turn_id), 
	CONSTRAINT uq_simulation_turn_number UNIQUE (simulation_id, turn_number), 
	CONSTRAINT uq_simulation_client_request UNIQUE (simulation_id, client_request_id), 
	FOREIGN KEY(simulation_id) REFERENCES simulation_sessions (simulation_id) ON DELETE CASCADE, 
	FOREIGN KEY(job_id) REFERENCES jobs (job_id) ON DELETE SET NULL, 
	FOREIGN KEY(context_snapshot_id) REFERENCES context_snapshots (snapshot_id) ON DELETE SET NULL
)

;

CREATE INDEX IF NOT EXISTS ix_chat_sessions_book_id ON chat_sessions (book_id);
CREATE INDEX IF NOT EXISTS ix_chat_sessions_updated_at ON chat_sessions (updated_at);
CREATE INDEX IF NOT EXISTS ix_jobs_book_id ON jobs (book_id);
CREATE INDEX IF NOT EXISTS ix_jobs_status ON jobs (status);
CREATE UNIQUE INDEX IF NOT EXISTS uq_active_job_per_book ON jobs (book_id) WHERE book_id IS NOT NULL AND lock_scope = 'book_write' AND status IN ('queued', 'running');
CREATE INDEX IF NOT EXISTS ix_long_term_memories_scope_id ON long_term_memories (scope_id);
CREATE INDEX IF NOT EXISTS ix_long_term_memories_scope_type ON long_term_memories (scope_type);
CREATE INDEX IF NOT EXISTS ix_long_term_memories_status ON long_term_memories (status);
CREATE INDEX IF NOT EXISTS ix_action_proposals_book_id ON action_proposals (book_id);
CREATE INDEX IF NOT EXISTS ix_action_proposals_book_status ON action_proposals (book_id, status);
CREATE INDEX IF NOT EXISTS ix_action_proposals_session_id ON action_proposals (session_id);
CREATE INDEX IF NOT EXISTS ix_action_proposals_session_status ON action_proposals (session_id, status);
CREATE INDEX IF NOT EXISTS ix_action_proposals_status ON action_proposals (status);
CREATE INDEX IF NOT EXISTS ix_chapter_runs_book_chapter_updated ON chapter_runs (book_id, chapter_number, updated_at);
CREATE INDEX IF NOT EXISTS ix_chapter_runs_book_id ON chapter_runs (book_id);
CREATE INDEX IF NOT EXISTS ix_chapter_runs_book_status_updated ON chapter_runs (book_id, status, updated_at);
CREATE INDEX IF NOT EXISTS ix_chapters_source_run_id ON chapters (source_run_id);
CREATE INDEX IF NOT EXISTS ix_chat_session_events_session_id ON chat_session_events (session_id);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_agent_role ON context_snapshots (agent_role);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_book_id ON context_snapshots (book_id);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_job_id ON context_snapshots (job_id);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_simulation_character_id ON context_snapshots (simulation_character_id);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_simulation_id ON context_snapshots (simulation_id);
CREATE INDEX IF NOT EXISTS ix_context_snapshots_simulation_turn_id ON context_snapshots (simulation_turn_id);
CREATE INDEX IF NOT EXISTS ix_job_events_job_id ON job_events (job_id);
CREATE INDEX IF NOT EXISTS ix_simulation_sessions_book_id ON simulation_sessions (book_id);
CREATE INDEX IF NOT EXISTS ix_simulation_sessions_status ON simulation_sessions (status);
CREATE INDEX IF NOT EXISTS ix_simulation_turns_job_id ON simulation_turns (job_id);
CREATE INDEX IF NOT EXISTS ix_simulation_turns_simulation_id ON simulation_turns (simulation_id);
CREATE UNIQUE INDEX IF NOT EXISTS uq_simulation_active_turn ON simulation_turns (simulation_id) WHERE status IN ('queued', 'running');

