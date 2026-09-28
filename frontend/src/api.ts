export type Json = Record<string, unknown>;

export interface TimelineEvent { event_id: string; sequence: number; event_type: string; created_at: string; payload: Json; }
export interface Session {
  session_id: string; title: string; book_id: string | null; message_count: number;
  messages: Array<Json & { sequence?: number }>; timeline: TimelineEvent[];
  paging: { has_more: boolean; next_before_sequence: number | null };
  selected_model?: ModelSelection | null;
  reasoning_level?: ReasoningLevel;
}
export interface ModelSelection { provider_id: string; model_id: string; }
export type ReasoningLevel = "default" | "off" | "low" | "medium" | "high" | "max";
export interface ModelProvider { id: string; name: string; base_url: string; models: string[]; has_api_key: boolean; reasoning_levels_by_model: Record<string, ReasoningLevel[]>; }
export interface ModelConfig { providers: ModelProvider[]; default_provider: string | null; default_model: string | null; }
export interface SessionSummary { session_id: string; title: string; book_id: string | null; message_count: number; updated_at: string; }
export interface ProjectSummary { book_id: string; title: string; genre: string; target_chapters: number; }
export interface SkillOption { id: string; name: string; display_name: string; description: string; short_description: string; }
export interface JobEvent { sequence: number; job_id: string; event_type: string; created_at: string; payload: Json; }
export interface Job { job_id: string; status: string; error: string | null; result: Json | null; }
export interface ActionProposal { action_proposal_id: string; action_type: string; summary: string; status: string; job_id: string | null; payload: Json; }
export interface Simulation { simulation_id: string; book_id: string; base_chapter_number: number; version: number; status: string; mode: string; user_character_id: string | null; current_turn: number; current_time: string; current_location: string; scene_summary: string; active_event: string | null; scene_status: string; characters: Json[]; }
export interface SimulationTurn { turn_id: string; turn_number: number; job_id: string | null; target_character_id?: string | null; input_type: string; user_input: string; status: string; output: Json | null; }

const ROOT = "/api/v1";
async function request<T>(path: string, options?: RequestInit): Promise<T> {
  const response = await fetch(`${ROOT}${path}`, { headers: { "Content-Type": "application/json", ...(options?.headers ?? {}) }, ...options });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(String(body.detail ?? body.error ?? `HTTP ${response.status}`));
  return body as T;
}

export const api = {
  modelConfig: () => request<ModelConfig>("/models/config"),
  saveModelProvider: (value: Omit<ModelProvider, "has_api_key" | "reasoning_levels_by_model"> & { api_key?: string; clear_key?: boolean }) => request<ModelConfig>(`/models/providers/${encodeURIComponent(value.id)}`, { method: "PUT", body: JSON.stringify(value) }),
  deleteModelProvider: (id: string) => request<ModelConfig>(`/models/providers/${encodeURIComponent(id)}`, { method: "DELETE" }),
  setDefaultModel: (value: ModelSelection) => request<ModelConfig>("/models/default", { method: "PUT", body: JSON.stringify(value) }),
  testModel: (value: ModelSelection) => request<{ ok: boolean }>("/models/test", { method: "POST", body: JSON.stringify(value) }),
  selectSessionModel: (id: string, value: ModelSelection) => request<ModelSelection>(`/sessions/${encodeURIComponent(id)}/model`, { method: "PUT", body: JSON.stringify(value) }),
  selectSessionReasoning: (id: string, level: ReasoningLevel) => request<{ level: ReasoningLevel }>(`/sessions/${encodeURIComponent(id)}/reasoning`, { method: "PUT", body: JSON.stringify({ level }) }),
  bootstrap: () => request<{ model: string; sessions: SessionSummary[]; projects: ProjectSummary[]; actions: Record<string, string>; skills?: SkillOption[] }>("/bootstrap"),
  createSession: (book_id: string | null = null) => request<Session>("/sessions", { method: "POST", body: JSON.stringify({ book_id }) }),
  deleteSession: (id: string) => request<{ ok: boolean }>(`/sessions/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getSession: (id: string, before?: number | null) => request<Session>(`/sessions/${encodeURIComponent(id)}?limit=50${before ? `&before_sequence=${before}` : ""}`),
  bindBook: (id: string, book_id: string | null) => request<Session>(`/sessions/${encodeURIComponent(id)}`, { method: "PATCH", body: JSON.stringify({ book_id }) }),
  send: (id: string, value: Json) => request<{ job_id: string }>(`/sessions/${encodeURIComponent(id)}/messages`, { method: "POST", body: JSON.stringify(value) }),
  retryAction: (id: string, runId: string) => request<{ job_id: string }>(`/sessions/${encodeURIComponent(id)}/actions/${encodeURIComponent(runId)}/retry`, { method: "POST" }),
  actionProposals: (id: string) => request<{ proposals: ActionProposal[] }>(`/sessions/${encodeURIComponent(id)}/action-proposals`),
  confirmActionProposal: (id: string, version?: number) => request<{ job_id: string }>(`/action-proposals/${encodeURIComponent(id)}/confirm`, {
    method: "POST",
    ...(version ? { body: JSON.stringify({ version }) } : {}),
  }),
  cancelActionProposal: (id: string) => request<{ proposal: ActionProposal }>(`/action-proposals/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  regenerateFoundationProposal: (id: string) => request<{ job_id: string }>(`/action-proposals/${encodeURIComponent(id)}/regenerate`, { method: "POST" }),
  updateFoundationProposal: (id: string, version: number, patch: Json) => request<{ proposal: ActionProposal }>(`/action-proposals/${encodeURIComponent(id)}/foundation`, {
    method: "PATCH", body: JSON.stringify({ version, patch }),
  }),
  createFoundationRevision: (bookId: string, sessionId: string, scope: "outline" | "setting") => request<{ proposal: ActionProposal }>(`/books/${encodeURIComponent(bookId)}/foundation-revisions`, {
    method: "POST", body: JSON.stringify({ session_id: sessionId, scope }),
  }),
  getProject: (id: string) => request<Json>(`/books/${encodeURIComponent(id)}`),
  getBook: (id: string) => request<Json>(`/books/${encodeURIComponent(id)}`),
  deleteBook: (id: string) => request<{ ok: boolean; book_id: string; unbound_session_ids: string[] }>(`/books/${encodeURIComponent(id)}`, { method: "DELETE" }),
  getCreativeControl: (id: string) => request<Json>(`/books/${encodeURIComponent(id)}/creative-control`),
  updateCreativeControl: (id: string, body: Json) => request<Json>(`/books/${encodeURIComponent(id)}/creative-control`, { method: "PUT", body: JSON.stringify(body) }),
  getChapter: (id: string, number: number) => request<Json>(`/books/${encodeURIComponent(id)}/chapters/${number}/content`),
  getTrace: (id: string) => request<{ trace: Json | null }>(`/sessions/${encodeURIComponent(id)}/trace`),
  memories: () => request<{ memories: Json[] }>("/memories"),
  disableMemory: (id: string) => request<{ memory: Json }>(`/memories/${encodeURIComponent(id)}`, { method: "DELETE" }),
  restoreMemory: (id: string) => request<{ memory: Json }>(`/memories/${encodeURIComponent(id)}/restore`, { method: "POST" }),
  deleteMemory: (id: string) => request<void>(`/memories/${encodeURIComponent(id)}/permanent`, { method: "DELETE" }),
  correctMemory: (id: string, body: Json) => request<{ memory: Json; replaced_memory_id: string }>(`/memories/${encodeURIComponent(id)}/corrections`, { method: "POST", body: JSON.stringify(body) }),
  getJob: (id: string) => request<Job>(`/jobs/${encodeURIComponent(id)}`),
  cancelJob: (id: string) => request<{ job_id: string }>(`/jobs/${encodeURIComponent(id)}/cancel`, { method: "POST" }),
  streamJob(id: string, callback: (event: JobEvent) => void) {
    const source = new EventSource(`${ROOT}/jobs/${encodeURIComponent(id)}/events`);
    const names = ["job_queued", "job_started", "job_succeeded", "job_failed", "job_paused", "job_interrupted", "job_cancelled", "stage_started", "stage_completed", "stage_failed", "model_completed", "tool_completed", "stream_started", "stream_completed", "preview_started", "preview_snapshot", "preview_delta", "preview_reset", "preview_completed", "simulation_context_ready", "simulation_model_started", "simulation_model_completed", "simulation_router_selected", "simulation_director_planned", "simulation_character_started", "simulation_character_preview", "simulation_message", "simulation_state_changed", "simulation_turn_committed", "simulation_turn_failed"];
    const receive = (message: MessageEvent) => callback(JSON.parse(message.data) as JobEvent);
    source.onmessage = receive;
    names.forEach((name) => source.addEventListener(name, receive as EventListener));
    return source;
  },
  simulations: (bookId: string) => request<{ simulations: Simulation[] }>(`/books/${encodeURIComponent(bookId)}/simulations`),
  createSimulation: (bookId: string, body: Json) => request<Simulation>(`/books/${encodeURIComponent(bookId)}/simulations`, { method: "POST", body: JSON.stringify(body) }),
  simulation: (id: string) => request<Simulation>(`/simulations/${encodeURIComponent(id)}`),
  simulationTurns: (id: string) => request<{ turns: SimulationTurn[] }>(`/simulations/${encodeURIComponent(id)}/turns`),
  submitSimulationTurn: (id: string, body: Json) => request<{ job_id: string }>(`/simulations/${encodeURIComponent(id)}/turns`, { method: "POST", body: JSON.stringify(body) }),
  continueSimulation: (id: string, body: Json) => request<{ job_id: string }>(`/simulations/${encodeURIComponent(id)}/continue`, { method: "POST", body: JSON.stringify(body) }),
  updateSimulationMode: (id: string, body: Json) => request<Simulation>(`/simulations/${encodeURIComponent(id)}/mode`, { method: "PATCH", body: JSON.stringify(body) }),
  simulationOperation: (id: string, operation: "pause" | "resume" | "finish") => request<Simulation>(`/simulations/${encodeURIComponent(id)}/${operation}`, { method: "POST" }),
};
