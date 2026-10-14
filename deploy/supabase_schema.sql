-- ==============================================================================
-- E-Cell Hiring Examination Portal - Supabase (PostgreSQL) Production Schema
-- Copy and paste this script into Supabase -> SQL Editor -> Click 'Run'
-- ==============================================================================

-- 1. Departments Table
CREATE TABLE IF NOT EXISTS public.departments (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);

-- 2. Users Table
CREATE TABLE IF NOT EXISTS public.users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('admin', 'applicant')),
    pw_salt BYTEA NOT NULL,
    pw_hash BYTEA NOT NULL,
    dept_id BIGINT REFERENCES public.departments(id) ON DELETE SET NULL,
    mobile TEXT NOT NULL DEFAULT '',
    contact_email TEXT NOT NULL DEFAULT '',
    has_changed_pw BOOLEAN NOT NULL DEFAULT FALSE,
    eligible BOOLEAN NOT NULL DEFAULT TRUE,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    last_login BIGINT
);

-- 3. Applicant Multi-Department Applications
CREATE TABLE IF NOT EXISTS public.applicant_departments (
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    dept_id BIGINT NOT NULL REFERENCES public.departments(id) ON DELETE CASCADE,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    PRIMARY KEY (user_id, dept_id)
);
CREATE INDEX IF NOT EXISTS idx_supabase_app_dept ON public.applicant_departments(dept_id);

-- 4. Applicant Verified Contact Info
CREATE TABLE IF NOT EXISTS public.applicant_contacts (
    user_id BIGINT PRIMARY KEY REFERENCES public.users(id) ON DELETE CASCADE,
    contact_email TEXT NOT NULL,
    mobile TEXT NOT NULL,
    updated_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);

-- 5. Sessions Table
CREATE TABLE IF NOT EXISTS public.sessions (
    token_hash TEXT PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    expires_at BIGINT NOT NULL
);

-- 6. Question Bank
CREATE TABLE IF NOT EXISTS public.questions (
    id BIGSERIAL PRIMARY KEY,
    category TEXT NOT NULL CHECK (category IN ('department', 'entrepreneurship')),
    dept_id BIGINT REFERENCES public.departments(id) ON DELETE SET NULL,
    text TEXT NOT NULL,
    opt1 TEXT NOT NULL,
    opt2 TEXT NOT NULL,
    opt3 TEXT NOT NULL,
    opt4 TEXT NOT NULL,
    correct INTEGER NOT NULL CHECK (correct BETWEEN 0 AND 3),
    difficulty TEXT NOT NULL CHECK (difficulty IN ('easy', 'medium', 'hard')),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    mandatory BOOLEAN NOT NULL DEFAULT FALSE,
    explanation TEXT NOT NULL DEFAULT '',
    created_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    CONSTRAINT chk_q_cat CHECK (
        (category = 'department' AND dept_id IS NOT NULL) OR 
        (category = 'entrepreneurship' AND dept_id IS NULL)
    ),
    CONSTRAINT chk_q_mand CHECK (mandatory = FALSE OR category = 'entrepreneurship')
);
CREATE INDEX IF NOT EXISTS idx_supabase_q_pool ON public.questions(category, dept_id, active, difficulty);

-- 7. Exam Configuration
CREATE TABLE IF NOT EXISTS public.config (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    status TEXT NOT NULL DEFAULT 'draft' CHECK (status IN ('draft', 'published')),
    duration_sec INTEGER NOT NULL DEFAULT 900,
    dept_count INTEGER NOT NULL DEFAULT 15,
    ent_count INTEGER NOT NULL DEFAULT 5,
    ent_mandatory_count INTEGER NOT NULL DEFAULT 3,
    min_easy INTEGER NOT NULL DEFAULT 5,
    min_medium INTEGER NOT NULL DEFAULT 3,
    min_hard INTEGER NOT NULL DEFAULT 2,
    allow_unanswered BOOLEAN NOT NULL DEFAULT TRUE,
    mandatory_must_answer BOOLEAN NOT NULL DEFAULT FALSE,
    max_attempts INTEGER NOT NULL DEFAULT 1,
    shuffle_options BOOLEAN NOT NULL DEFAULT TRUE,
    result_visible BOOLEAN NOT NULL DEFAULT FALSE,
    show_explanations BOOLEAN NOT NULL DEFAULT FALSE,
    opens_at BIGINT,
    closes_at BIGINT,
    instructions TEXT NOT NULL DEFAULT ''
);

-- 8. Instructions & Markdown Rules
CREATE TABLE IF NOT EXISTS public.instructions (
    id INTEGER PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    version INTEGER NOT NULL DEFAULT 1,
    title TEXT NOT NULL DEFAULT 'E-Cell Hiring Examination: Official Rules & Instructions',
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'published' CHECK (status IN ('draft', 'published')),
    updated_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    published_at BIGINT DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);

-- 9. Exam Attempts
CREATE TABLE IF NOT EXISTS public.attempts (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    dept_id BIGINT NOT NULL REFERENCES public.departments(id) ON DELETE CASCADE,
    started_at BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT),
    deadline_at BIGINT NOT NULL,
    submitted_at BIGINT,
    status TEXT NOT NULL DEFAULT 'in_progress' CHECK (status IN ('in_progress', 'submitted', 'timed_out')),
    correct_count INTEGER,
    incorrect_count INTEGER,
    unanswered_count INTEGER,
    total_questions INTEGER,
    percent DOUBLE PRECISION,
    duration_sec INTEGER,
    instructions_version INTEGER NOT NULL DEFAULT 1,
    acknowledged_at BIGINT
);
-- 1 attempt per department per applicant constraint
CREATE UNIQUE INDEX IF NOT EXISTS one_attempt_per_user_dept ON public.attempts(user_id, dept_id);
-- Only 1 active exam in progress across any department
CREATE UNIQUE INDEX IF NOT EXISTS one_active_attempt ON public.attempts(user_id) WHERE status = 'in_progress';

-- 10. Attempt Question Instances & Shuffled Order
CREATE TABLE IF NOT EXISTS public.attempt_questions (
    attempt_id BIGINT NOT NULL REFERENCES public.attempts(id) ON DELETE CASCADE,
    position INTEGER NOT NULL,
    question_id BIGINT NOT NULL REFERENCES public.questions(id) ON DELETE CASCADE,
    option_order TEXT NOT NULL,
    answer_pos INTEGER CHECK (answer_pos IS NULL OR answer_pos BETWEEN 0 AND 3),
    flagged BOOLEAN NOT NULL DEFAULT FALSE,
    answered_at BIGINT,
    PRIMARY KEY (attempt_id, position),
    UNIQUE (attempt_id, question_id)
);

-- 11. Security & Anti-Cheat Audit Events
CREATE TABLE IF NOT EXISTS public.security_events (
    id BIGSERIAL PRIMARY KEY,
    attempt_id BIGINT REFERENCES public.attempts(id) ON DELETE CASCADE,
    user_id BIGINT NOT NULL REFERENCES public.users(id) ON DELETE CASCADE,
    type TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ts BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);
CREATE INDEX IF NOT EXISTS idx_supabase_ev_attempt ON public.security_events(attempt_id, ts);

-- 12. Administrative Audit Logs
CREATE TABLE IF NOT EXISTS public.audit_logs (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES public.users(id) ON DELETE SET NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT '',
    ip TEXT NOT NULL DEFAULT '',
    ts BIGINT NOT NULL DEFAULT (EXTRACT(EPOCH FROM NOW())::BIGINT)
);
CREATE INDEX IF NOT EXISTS idx_supabase_audit_ts ON public.audit_logs(ts);

-- -----------------------------------------------------------------------------
-- INITIAL SEED DATA
-- -----------------------------------------------------------------------------
INSERT INTO public.departments (name) VALUES
    ('Design'), ('Technical'), ('Event Management'), ('Content'), ('Media'),
    ('Marketing'), ('PR and HR'), ('Sponsorship'), ('Incubation and Innovation'), ('Communication')
ON CONFLICT (name) DO NOTHING;

INSERT INTO public.config (id, status, duration_sec) VALUES (1, 'published', 900)
ON CONFLICT (id) DO NOTHING;

INSERT INTO public.instructions (id, version, title, content, status) VALUES 
(1, 1, 'E-Cell Hiring Examination: Official Rules & Instructions', 
 'The exam has a fixed time limit that runs on the server. Closing the page does not pause it. Questions and option order are random and fixed once you start. Leaving fullscreen, tab switching, or copying is recorded for the review team. Do not use AI tools. Answers save automatically.', 
 'published')
ON CONFLICT (id) DO NOTHING;
