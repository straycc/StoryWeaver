export type Json = Record<string, unknown>;

export interface TimelineEvent { event_id: string; sequence: number; event_type: string; created_at: string; payload: Json; }
export interface Session {
  session_id: string; title: string; book_id: string | null; message_count: number;
  messages: Array<Json & { sequence?: number }>; timeline: TimelineEvent[];
  paging: { has_more: boolean; next_before_sequence: number | null };
}
export interface SessionSummary { session_id: string; title: string; book_id: string | null; message_count: number; updated_at: string; }
export interface ProjectSummary { book_id: string; title: string; genre: string; target_chapters: number; }
export interface JobEvent { sequence: number; job_id: string; event_type: string; created_at: string; payload: Json; }
export interface Job { job_id: string; status: string; error: string | null; result: Json | null; }
export interface ActionProposal { action_proposal_id: string; action_type: string; summary: string; status: string; job_id: string | null; payload: Json; }

const ROOT = "/api/v1";
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, { headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(body.detail ?? body.error ?? `HTTP ${response.status}`));
  return body as T;
}

export const api = {
  bootstrap: () => request<{ model: string; sessions: SessionSummary[]; projects: ProjectSummary[]; actions: Record<string, string> }>("/bootstrap"),
  createSession: (book_id: string | null = null) => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ book_id }) }),
  getSession: (id: string, before?: number | null) => request<Session>(`/sessions/${encodeURIComponent(id)}?limit=50${before ? `&before_sequence=${before}` : ""}`),
  bindBook: (id: string, book_id: string | null) => request<Session>(`/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ book_id }) }),
  send: (id: string, value: Json) => request<{ job_id: string }>(`/sessions/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify(value) }),
  retryAction: (id: string, runId: string) => request<{ job_id: string }>(`/sessions/${encodeURIComponent(id)}/actions/${encodeURIComponent(runId)}/retry`, { method: "POST" }),
  actionProposals: (id: string) => request<{ proposals: ActionProposal[] }>(`/sessions/${encodeURIComponent(id)}/action-proposals`),
  confirmActionProposal: (id: string) => request<{ job_id: string }>(`/action-proposals/${encodeURIComponent(id)}/confirm`, { method: "POST" }),
  cancelActionProposal: (id: string) => request<{ proposal: ActionProposal }>(`/action-proposals/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  getProject: (id: string) => request<Json>(`/projects/${encodeURIComponent(id)}`),
  getCreativeControl: (id: string) => request<Json>(`/books/${encodeURIComponent(id)}/creative-control`),
  updateCreativeControl: (id: string, body: Json) => request<Json>(`/books/${encodeURIComponent(id)}/creative-control`, { method: "PUT", body: JSON.stringify(body) }),
  getChapter: (id: string, number: number) => request<Json>(`/projects/${encodeURIComponent(id)}/chapters/${number}`),
  getTrace: (id: string) => request<{ trace: Json | null }>(`/sessions/${encodeURIComponent(id)}/trace`),
  memories: () => request<{ memories: Json[] }>("/memories"),
  updateMemory: (id: string, body: Json) => request<{ memory: Json }>(`/memories/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify(body) }),
  disableMemory: (id: string) => request<{ memory: Json }>(`/memories/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restoreMemory: (id: string) => request<{ memory: Json }>(`/memories/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  getJob: (id: string) => request<Job>(`/jobs/${encodeURIComponent(id)}`),
  streamJob(id: string, callback: (event: JobEvent) => void) {
    const source = new EventSource(`${ROOT}/jobs/${encodeURIComponent(id)}/events`);
    const names = ["job_queued", "job_started", "job_succeeded", "job_failed", "job_paused", "job_interrupted", "stage_started", "stage_completed", "stage_failed", "model_completed", "tool_completed"];
    const receive = (message: MessageEvent) => callback(JSON.parse(message.data) as JobEvent);
    source.onmessage = receive;
    names.forEach((name) => source.addEventListener(name, receive as EventListener));
    return source;
  },
};
