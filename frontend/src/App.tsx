import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, type ActionProposal, type Json, type ProjectSummary, type Session, type TimelineEvent } from "./api";

const DEFAULT_BOOK: Json = { title: "雨夜旅馆", genre: "悬疑", premise: "年轻侦探林默进入废弃旅馆，调查十年前发生的失踪案。", protagonist: "林默", tone: "克制、压迫、有限视角", central_conflict: "林默寻找真相，旅馆中的神秘人试图把他引向错误线索。", target_chapters: 6, chapter_target_words: 1200, language: "zh" };
const HOOK_STATUS: Record<string, string> = { open: "未解", progressing: "推进中", resolved: "已回收", deferred: "暂缓" };
const CHAPTER_STATUS: Record<string, string> = { ready_for_review: "审查通过，待人工复阅", review_warning: "审查通过（有提示）", draft_rejected: "审查未通过" };
const obj = (value: unknown): Json => value && typeof value === "object" ? value as Json : {};
const text = (value: unknown) => typeof value === "string" ? value : value == null ? "" : String(value);
const list = (value: unknown): unknown[] => Array.isArray(value) ? value : [];

export default function App() {
  const [bootstrap, setBootstrap] = useState<{ model: string; sessions: any[]; projects: ProjectSummary[]; actions: Record<string, string>; skills?: Array<{ id: string; name: string; description: string }> } | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [paging, setPaging] = useState({ has_more: false, next_before_sequence: null as number | null });
  const [view, setView] = useState<"chat" | "work">("chat");
  const [viewingBookId, setViewingBookId] = useState<string | null>(null);
  const [project, setProject] = useState<Json | null>(null);
  const [chapter, setChapter] = useState<Json | null>(null);
  const [activeAction, setActiveAction] = useState("chat");
  const [busy, setBusy] = useState(false);
  const [createOpen, setCreateOpen] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [controlOpen, setControlOpen] = useState(false);
  const [creativeControl, setCreativeControl] = useState<Json | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [memories, setMemories] = useState<Json[]>([]);
  const [trace, setTrace] = useState<Json | null>(null);
  const [toast, setToast] = useState<{ message: string; error: boolean } | null>(null);
  const [jobEvents, setJobEvents] = useState<Record<string, any[]>>({});
  const [input, setInput] = useState("");
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const [skillMenuIndex, setSkillMenuIndex] = useState(0);
  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSources = useRef<Map<string, EventSource>>(new Map());

  const notify = useCallback((message: string, error = false) => {
    setToast({ message, error }); window.setTimeout(() => setToast(null), 3200);
  }, []);
  const refreshBootstrap = useCallback(async () => setBootstrap(await api.bootstrap()), []);
  const loadSession = useCallback(async (id: string, before?: number | null) => {
    const data = await api.getSession(id, before);
    setSession(data);
    setViewingBookId(data.book_id);
    if (before) setTimeline((old) => mergeTimeline([...data.timeline, ...old])); else setTimeline(data.timeline);
    setPaging(data.paging);
    return data;
  }, []);
  const createSession = useCallback(async (bookId: string | null = null) => {
    const data = await api.createSession(bookId);
    setSession(data); setTimeline([]); setPaging({ has_more: false, next_before_sequence: null });
    setViewingBookId(bookId); setActiveAction("chat"); await refreshBootstrap(); return data;
  }, [refreshBootstrap]);

  useEffect(() => {
    void (async () => {
      try { await refreshBootstrap(); const data = await api.bootstrap(); if (data.sessions[0]) await loadSession(data.sessions[0].session_id); else await createSession(); }
      catch (error) { notify(errorMessage(error), true); }
    })();
    return () => eventSources.current.forEach((item) => item.close());
  }, [createSession, loadSession, notify, refreshBootstrap]);
  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }); }, [timeline, jobEvents]);

  const currentBookId = viewingBookId || session?.book_id || null;
  const projectTitle = (id: string | null | undefined) => bootstrap?.projects.find((item) => item.book_id === id)?.title || id || "";
  const sessionHasActivity = timeline.some((event) => ["message_added", "action_started", "action_completed", "action_failed", "tool_called", "tool_result"].includes(event.event_type));

  const loadProject = useCallback(async (bookId: string | null) => {
    if (!bookId) { setProject(null); return; }
    try { setProject(await api.getProject(bookId)); } catch (error) { notify(errorMessage(error), true); }
  }, [notify]);
  useEffect(() => { if (view === "work") void loadProject(currentBookId); }, [view, currentBookId, loadProject]);
  const openCreativeControl = async () => {
    if (!currentBookId) { notify("请先选择一部作品", true); return; }
    try { setCreativeControl(await api.getCreativeControl(currentBookId)); setControlOpen(true); }
    catch (error) { notify(errorMessage(error), true); }
  };

  const watchJob = useCallback((jobId: string, sessionId: string) => {
    eventSources.current.get(jobId)?.close();
    const source = api.streamJob(jobId, (event) => {
      setJobEvents((old) => ({ ...old, [jobId]: [...(old[jobId] || []), event] }));
      if (["job_succeeded", "job_failed", "job_paused", "job_interrupted"].includes(event.event_type)) {
        source.close(); eventSources.current.delete(jobId);
        if (event.event_type === "job_succeeded" && typeof obj(event.payload.result).job_id === "string") {
          watchJob(text(obj(event.payload.result).job_id), sessionId);
        } else {
          setBusy(false);
          void loadSession(sessionId).then(() => refreshBootstrap()).then(() => loadProject(currentBookId)).catch((error) => notify(errorMessage(error), true));
        }
      }
    });
    eventSources.current.set(jobId, source);
    void api.getJob(jobId).then((item) => { if (["failed", "paused", "interrupted"].includes(item.status)) notify(item.error || "任务失败", true); });
  }, [currentBookId, loadProject, loadSession, notify, refreshBootstrap]);

  const send = useCallback(async (action = activeAction, content = input, payload: Json = {}) => {
    if (!session || busy) return;
    const normalized = content.trim() || (bootstrap?.actions[action] || action);
    if (!normalized && action === "chat") return;
    setBusy(true); setInput("");
    try {
      const result = await api.send(session.session_id, { content: normalized, action, book_id: session.book_id, payload });
      // HTTP 只确认 Job 已入队。先在本地补一对时间线事件，让用户不必等
      // Planner 完整结束才看到“写下一章”及其实时模型/工具进度。
      const maximum = timeline.reduce((value, item) => Math.max(value, item.sequence), 0);
      const now = new Date().toISOString();
      const label = bootstrap?.actions[action] || action;
      setTimeline((old) => mergeTimeline([
        ...old,
        {
          event_id: `local-message-${result.job_id}`,
          sequence: maximum + 1,
          event_type: "message_added",
          created_at: now,
          payload: { role: "user", content: normalized, action, metadata: {} },
        },
        {
          event_id: `local-action-${result.job_id}`,
          sequence: maximum + 2,
          event_type: "action_started",
          created_at: now,
          payload: { run_id: result.job_id, root_run_id: result.job_id, action, label },
        },
      ]));
      watchJob(result.job_id, session.session_id);
      if (action !== "revise_chapter_plan") setActiveAction("chat");
    } catch (error) { setBusy(false); notify(errorMessage(error), true); }
  }, [activeAction, bootstrap?.actions, busy, input, notify, session, timeline, watchJob]);

  const bindBook = async (bookId: string | null) => {
    if (!session) return;
    if (session.book_id === bookId) return;
    try {
      if (sessionHasActivity) {
        if (!window.confirm(`当前对话已有内容，不能原地切换作品。\n\n是否创建一个绑定《${projectTitle(bookId)}》的新对话？`)) return;
        await createSession(bookId);
      } else {
        const data = await api.bindBook(session.session_id, bookId);
        setSession((old) => old ? { ...old, ...data } : data); setViewingBookId(bookId); await refreshBootstrap();
      }
    } catch (error) { notify(errorMessage(error), true); }
  };
  const choosePreset = (preset: string) => {
    if (preset === "create") { setCreateOpen(true); return; }
    if (preset === "start_chapter_batch") { setBatchOpen(true); return; }
    if (preset === "project_status") { setView("work"); return; }
    setActiveAction(preset); document.querySelector<HTMLTextAreaElement>("#message-input")?.focus();
  };
  const ensureChatForBook = async () => {
    if (!currentBookId) { notify("请先选择一部作品", true); return false; }
    if (session?.book_id !== currentBookId) await bindBook(currentBookId);
    return true;
  };
  const openMemories = async () => { setMemoryOpen(true); try { setMemories((await api.memories()).memories); } catch (error) { notify(errorMessage(error), true); } };
  const openTrace = async () => { if (!session) return; setTraceOpen(true); setTrace((await api.getTrace(session.session_id)).trace); };
  const pendingPlan = useMemo(() => [...timeline].reverse().find((event) => event.event_type.startsWith("chapter_plan_") && text(event.payload.status) === "pending")?.payload ?? null, [timeline]);
  const visibleSkills = useMemo(() => (bootstrap?.skills || []).filter((item) => input.trim() === "/" || item.id.startsWith(input.trim().slice(1).toLowerCase())), [bootstrap?.skills, input]);
  const selectSkill = (skillId: string) => {
    setInput(`/skill ${skillId} `);
    setSkillMenuOpen(false);
    setSkillMenuIndex(0);
    window.setTimeout(() => document.querySelector<HTMLTextAreaElement>("#message-input")?.focus(), 0);
  };

  return <div className="app-shell">
    <aside className="sidebar" id="sidebar">
      <div className="brand-row"><div className="brand-mark">S</div><div><div className="brand-name">StoryWeaver</div><div className="brand-subtitle">小说工作台</div></div><button className="icon-button sidebar-close" aria-label="关闭侧栏">×</button></div>
      <button className="new-chat-button" onClick={() => void createSession()}><span>＋</span><span>新建对话</span></button>
      <nav className="sidebar-nav"><section className="nav-group"><div className="section-heading">创作</div><button className="nav-item" onClick={() => choosePreset("write_next")}><span className="nav-icon">↗</span><span>写下一章</span></button><button className="nav-item" onClick={() => choosePreset("start_chapter_batch")}><span className="nav-icon">≋</span><span>连续创作</span></button></section><section className="nav-group"><div className="section-heading">资料</div><button className="nav-item" onClick={() => void openMemories()}><span className="nav-icon">◇</span><span>长期记忆</span></button><button className="nav-item" onClick={() => void openCreativeControl()}><span className="nav-icon">◌</span><span>创作控制</span></button></section></nav>
      <section className="sidebar-section"><div className="section-heading section-heading-row"><span>我的作品</span><button className="section-add-button" onClick={() => setCreateOpen(true)}>＋</button></div><div className="compact-list">{bootstrap?.projects.length ? bootstrap.projects.map((item) => <button key={item.book_id} className={currentBookId === item.book_id ? "compact-item active" : "compact-item"} onClick={() => { setViewingBookId(item.book_id); setView("work"); }}><strong>{item.title}</strong><span>{item.genre} · {item.target_chapters} 章</span></button>) : <div className="empty-list">还没有作品</div>}</div></section>
      <section className="sidebar-section conversations-section"><div className="section-heading">最近对话</div><div className="compact-list">{bootstrap?.sessions.length ? bootstrap.sessions.map((item) => <button key={item.session_id} className={session?.session_id === item.session_id ? "compact-item active" : "compact-item"} onClick={() => void loadSession(item.session_id)}><strong>{item.title}</strong><span>{item.message_count} 条消息 · {item.book_id ? `《${projectTitle(item.book_id)}》` : "未绑定作品"}</span></button>) : <div className="empty-list">还没有历史对话</div>}</div></section>
      <div className="sidebar-footer"><div className="model-dot"></div><div className="footer-copy"><strong>{bootstrap?.model || "加载中"}</strong><span>PostgreSQL 工作区</span></div></div>
    </aside>
    <main className="main-panel">
      <header className="topbar"><button className="icon-button mobile-menu">☰</button><div className="view-switcher"><button className={view === "chat" ? "view-tab active" : "view-tab"} onClick={() => setView("chat")}>聊天</button><button className={view === "work" ? "view-tab active" : "view-tab"} onClick={() => setView("work")}>作品</button></div><div className="topbar-actions"><label className="project-picker-label">当前会话绑定</label><select className="project-picker" value={session?.book_id || ""} onChange={(event) => void bindBook(event.target.value || null)}><option value="">未关联作品</option>{bootstrap?.projects.map((item) => <option key={item.book_id} value={item.book_id}>{item.title}</option>)}</select></div></header>
      <section className={view === "chat" ? "view-panel chat-view active" : "view-panel chat-view"}><div className="message-scroll" ref={scrollRef}>
        {!timeline.length && <div className="empty-state"><h1>准备好了，随时开始</h1><p>讨论灵感、创建小说，或者继续当前作品。</p><div className="starter-grid"><button onClick={() => setCreateOpen(true)}><strong>创建小说</strong><span>从创作简报生成世界、角色和大纲</span></button><button onClick={() => choosePreset("write_next")}><strong>写下一章</strong><span>点击这里，或直接输入“写下一章”</span></button><button onClick={() => setView("work")}><strong>查看状态</strong><span>检查人物、事实、伏笔和章节进度</span></button><button onClick={() => setActiveAction("chat")}><strong>讨论灵感</strong><span>和编辑助手聊人物、冲突与写作方向</span></button></div></div>}
        {paging.has_more && <div className="load-more-wrap"><button className="secondary-button" onClick={() => session && void loadSession(session.session_id, paging.next_before_sequence)}>加载更早记录</button></div>}
        <div className="message-list">{mergeTimeline(timeline).map((event) => <TimelineCard key={event.event_id} event={event} actions={bootstrap?.actions || {}} progress={jobEvents[text(event.payload.run_id)] || []} onConfirm={(id) => void send("confirm_chapter_plan", "确认候选计划并生成本章", { proposal_id: id })} onCancel={(id) => void send("cancel_chapter_plan", "取消候选章节计划", { proposal_id: id })} onRevise={(id) => { setActiveAction("revise_chapter_plan"); setInput(""); (window as any).__proposalId = id; }} onConfirmAction={(id) => session && void api.confirmActionProposal(id).then((item) => watchJob(item.job_id, session.session_id)).catch((error) => notify(errorMessage(error), true))} onCancelAction={(id) => void api.cancelActionProposal(id).then(() => session && loadSession(session.session_id)).catch((error) => notify(errorMessage(error), true))} onRetry={(id) => session && void api.retryAction(session.session_id, id).then((item) => watchJob(item.job_id, session.session_id))} onTrace={() => void openTrace()} onSuggestion={(label) => void send("chat", label)} />)}</div>
      </div><div className="composer-wrap">{activeAction !== "chat" && <div className="active-action"><span>{bootstrap?.actions[activeAction] || activeAction}</span><button onClick={() => setActiveAction("chat")}>×</button></div>}<div className="composer-command-wrap">{skillMenuOpen && <div className="skill-command-menu" role="listbox" aria-label="可用创作 Skill">{visibleSkills.length ? visibleSkills.map((skill, index) => <button type="button" role="option" aria-selected={index === skillMenuIndex} className={index === skillMenuIndex ? "active" : ""} key={skill.id} onMouseDown={(event) => { event.preventDefault(); selectSkill(skill.id); }}><strong>/{skill.id}</strong><span>{skill.description}</span></button>) : <div className="skill-command-empty">没有匹配的创作 Skill</div>}</div>}<form className="composer" onSubmit={(event) => { event.preventDefault(); const payload = activeAction === "revise_chapter_plan" ? { proposal_id: (window as any).__proposalId || pendingPlan?.proposal_id } : {}; void send(activeAction, input, payload); }}><textarea id="message-input" rows={1} value={input} onChange={(event) => { const value = event.target.value; setInput(value); setSkillMenuOpen(/^\/[^\s]*$/.test(value)); setSkillMenuIndex(0); }} onBlur={() => window.setTimeout(() => setSkillMenuOpen(false), 120)} onKeyDown={(event) => { if (skillMenuOpen && visibleSkills.length) { if (event.key === "ArrowDown") { event.preventDefault(); setSkillMenuIndex((index) => (index + 1) % visibleSkills.length); return; } if (event.key === "ArrowUp") { event.preventDefault(); setSkillMenuIndex((index) => (index - 1 + visibleSkills.length) % visibleSkills.length); return; } if ((event.key === "Enter" || event.key === "Tab") && !event.shiftKey) { event.preventDefault(); selectSkill(visibleSkills[skillMenuIndex].id); return; } if (event.key === "Escape") { event.preventDefault(); setSkillMenuOpen(false); return; } } if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); event.currentTarget.form?.requestSubmit(); } }} placeholder={activeAction === "write_next" ? "可以补充本章要求；直接发送则按当前计划继续……" : activeAction === "revise_chapter_plan" ? "告诉 StoryWeaver 希望如何调整本章计划……" : "输入消息，和 StoryWeaver 一起创作……"} /><div className="composer-footer"><div className="composer-tools"><button type="button" className="tool-button" onClick={() => setCreateOpen(true)}>＋</button><span>Enter 发送 · Shift+Enter 换行 · 输入 / 选择 Skill</span></div><button type="submit" className="send-button" disabled={busy}>↑</button></div></form></div><p className="safety-note">生成内容可能存在偏差，重要设定请检查后再继续写作。</p></div></section>
      <section className={view === "work" ? "view-panel work-view active" : "view-panel work-view"}><WorkView project={project} chapter={chapter} bound={session?.book_id === currentBookId} onWrite={async () => { if (await ensureChatForBook()) { setView("chat"); choosePreset("write_next"); } }} onBatch={() => setBatchOpen(true)} onChapter={async (number) => { if (currentBookId) setChapter(await api.getChapter(currentBookId, number)); }} onCloseChapter={() => setChapter(null)} onRewrite={() => setRewriteOpen(true)} /></section>
    </main>
    {createOpen && <CreateDialog onClose={() => setCreateOpen(false)} onSubmit={(payload: Json) => { setCreateOpen(false); void (async () => { if (sessionHasActivity || session?.book_id) await createSession(); await send("create_novel", `创建小说《${text(payload.title)}》`, payload); })(); }} />}
    {batchOpen && <BatchDialog onClose={() => setBatchOpen(false)} onSubmit={(payload: Json) => { setBatchOpen(false); void (async () => { if (await ensureChatForBook()) { setView("chat"); await send("start_chapter_batch", `连续创作 ${payload.chapter_count} 章`, payload); } })(); }} />}
    {rewriteOpen && <RewriteDialog project={project} chapter={chapter} onClose={() => setRewriteOpen(false)} onSubmit={(payload: Json) => { setRewriteOpen(false); setView("chat"); void send("rewrite_chapter", `重写第${payload.chapter_number}章${payload.instruction ? `：${payload.instruction}` : ""}`, payload); }} />}
    <div className={memoryOpen || traceOpen || controlOpen ? "drawer-backdrop show" : "drawer-backdrop"} onClick={() => { setMemoryOpen(false); setTraceOpen(false); setControlOpen(false); }}></div>
    {memoryOpen && <MemoryDrawer memories={memories} bookId={session?.book_id || null} onClose={() => setMemoryOpen(false)} onUpdate={async (id: string, data: Json) => { const result = await api.updateMemory(id, data); setMemories((old) => old.map((item) => text(item.memory_id) === id ? result.memory : item)); }} onToggle={async (id: string, restore: boolean) => { const result = restore ? await api.restoreMemory(id) : await api.disableMemory(id); setMemories((old) => old.map((item) => text(item.memory_id) === id ? result.memory : item)); }} />}
    {traceOpen && <TraceDrawer trace={trace} onClose={() => setTraceOpen(false)} />}
    {controlOpen && creativeControl && <CreativeControlDrawer control={creativeControl} onClose={() => setControlOpen(false)} onSave={async (value: Json) => { if (!currentBookId) return; const updated = await api.updateCreativeControl(currentBookId, value); setCreativeControl(updated); setProject((old) => old ? { ...old, creative_control: updated } : old); notify("创作控制已保存，将从下一次规划或写作开始生效。"); }} />}
    {toast && <div className={toast.error ? "toast error show" : "toast show"}>{toast.message}</div>}
  </div>;
}

function TimelineCard({ event, actions, progress, onConfirm, onCancel, onRevise, onConfirmAction, onCancelAction, onRetry, onTrace, onSuggestion }: { event: TimelineEvent; actions: Record<string, string>; progress: any[]; onConfirm: (id: string) => void; onCancel: (id: string) => void; onRevise: (id: string) => void; onConfirmAction: (id: string) => void; onCancelAction: (id: string) => void; onRetry: (id: string) => void; onTrace: () => void; onSuggestion: (label: string) => void }) {
  const payload = event.payload || {};
  if (event.event_type === "message_added") { const suggestions = Array.isArray(obj(payload.metadata).suggested_actions) ? obj(payload.metadata).suggested_actions as Json[] : []; return <article className={payload.role === "user" ? "message user" : "message assistant"}><div className="message-content">{payload.role !== "user" && <div className="assistant-label">StoryWeaver</div>}<Text value={text(payload.content)} />{payload.role !== "user" && Boolean(obj(payload.metadata).context_trace) && <div className="message-meta"><button className="trace-link" onClick={onTrace}>查看本轮上下文</button></div>}{payload.role !== "user" && suggestions.length > 0 && <div className="suggestion-row">{suggestions.slice(0, 3).map((item, index) => <button key={`${text(item.label)}-${index}`} onClick={() => onSuggestion(text(item.label))}>{text(item.label)}</button>)}</div>}</div></article>; }
  if (event.event_type === "book_bound") return <div className="binding-divider"><span>已切换至作品</span></div>;
  if (event.event_type === "story_timeline_rewritten") return <div className="binding-divider"><span>时间线已归档，当前章节重新开始</span></div>;
  if (event.event_type.startsWith("chapter_plan_") && !["cancelled", "expired"].includes(text(payload.status))) return <PlanCard plan={payload} onConfirm={onConfirm} onCancel={onCancel} onRevise={onRevise} />;
  if (event.event_type === "action_proposal_pending") return <ActionProposalCard proposal={payload as unknown as ActionProposal} onConfirm={onConfirmAction} onCancel={onCancelAction} />;
  if (event.event_type === "action_proposal_confirmed") return <div className="binding-divider"><span>已确认操作，正在执行</span></div>;
  if (event.event_type === "action_proposal_cancelled") return <div className="binding-divider"><span>已取消待确认操作</span></div>;
  if (event.event_type.startsWith("action_")) {
    const detail = text(payload.error) || text(payload.summary) || (event.event_type === "action_started" ? "正在处理预设动作……" : event.event_type === "action_failed" ? "操作失败。" : "操作已完成。");
    return <details className={event.event_type === "action_failed" ? "event-card failed" : "event-card"} open={event.event_type === "action_started"}><summary><span>{text(payload.label) || actions[text(payload.action)] || text(payload.action) || "创作动作"}</span><em>{event.event_type === "action_started" ? "执行中" : event.event_type === "action_failed" ? "失败" : "已完成"}</em></summary><div className="event-detail"><RunProgress events={progress} />{detail}{event.event_type === "action_failed" && text(payload.run_id) && <button className="event-retry" onClick={() => onRetry(text(payload.run_id))}>重试</button>}</div></details>;
  }
  return null;
}
function ActionProposalCard({ proposal, onConfirm, onCancel }: { proposal: ActionProposal; onConfirm: (id: string) => void; onCancel: (id: string) => void }) {
  const id = text(proposal.action_proposal_id);
  return <article className="chapter-plan-card pending action-proposal-card"><header><div><span className="plan-kicker">ACTION CONFIRMATION</span><h3>需要你的确认</h3></div><span className="plan-status">等待确认</span></header><p>{text(proposal.summary)}</p><div className="plan-actions"><button className="primary-button" onClick={() => onConfirm(id)}>确认并执行</button><button className="plan-cancel" onClick={() => onCancel(id)}>取消</button></div></article>;
}
function Text({ value }: { value: string }) { return <>{value.split("\n").map((line, index) => <span key={index}>{line}{index < value.split("\n").length - 1 && <br />}</span>)}</>; }
function RunProgress({ events }: { events: any[] }) {
  if (!events?.length) return <div className="run-progress-empty"><span className="progress-spinner"></span>正在启动 Agent Pipeline…</div>;
  const stages: Array<{ agentId: string; name: string; status: "running" | "completed" | "failed"; elapsed?: number; tokens?: number; error?: string; models: any[]; tools: any[] }> = [];
  for (const event of events) {
    const payload = obj(event.payload);
    if (event.event_type === "stage_started") {
      stages.push({ agentId: text(payload.agent_id), name: text(payload.display_name) || text(payload.agent_id) || "Agent", status: "running", models: [], tools: [] });
      continue;
    }
    const stage = [...stages].reverse().find((item) => item.agentId === text(payload.agent_id) && item.status === "running");
    if (!stage) continue;
    if (event.event_type === "model_completed") stage.models.push(payload);
    if (event.event_type === "tool_completed") stage.tools.push(payload);
    if (event.event_type === "stage_completed" || event.event_type === "stage_failed") {
      stage.status = event.event_type === "stage_failed" ? "failed" : "completed";
      stage.elapsed = Number(payload.elapsed_seconds || 0);
      stage.tokens = Number(payload.total_tokens || 0);
      stage.error = text(payload.error);
    }
  }
  return <ol className="run-progress-list">{stages.map((stage, index) => <li key={`${stage.agentId}-${index}`} className={`run-stage ${stage.status}`}>
    <span className="run-stage-icon">{stage.status === "completed" ? "✓" : stage.status === "failed" ? "×" : <i className="progress-spinner" />}</span>
    <span className="run-stage-name">{stage.name}</span>
    <span className="run-stage-metrics">{stage.elapsed ? `${stage.elapsed.toFixed(1)}s` : ""}{stage.elapsed && stage.tokens ? " · " : ""}{stage.tokens ? `${stage.tokens.toLocaleString()} Token` : stage.status === "running" ? "执行中" : ""}</span>
    {stage.error && <small>{stage.error}</small>}
    {stage.models.length > 0 && <ul className="run-model-list">{stage.models.map((model, modelIndex) => <li key={modelIndex}>{formatModelEvent(model)}</li>)}</ul>}
    {stage.tools.length > 0 && <ul className="run-tool-list">{stage.tools.map((tool, toolIndex) => <li key={toolIndex} className={tool.succeeded === false ? "failed" : ""}>{formatToolEvent(tool)}</li>)}</ul>}
  </li>)}</ol>;
}

function formatModelEvent(payload: Json): string {
  const step = Number(payload.step || 0), maximum = Number(payload.max_steps || 0);
  const round = step && maximum ? `第 ${step}/${maximum} 回合` : "模型回合";
  const kind = text(payload.response_kind);
  const result = kind === "research" ? "检索证据" : kind === "tool_calls" ? `请求工具 ${Number(payload.tool_call_count || 0)} 个` : kind === "final" ? "输出最终结果" : "未返回有效结果";
  return `${round} · ${result} · 输入 ${Number(payload.input_tokens || 0).toLocaleString()} · 输出 ${Number(payload.output_tokens || 0).toLocaleString()}${Number(payload.elapsed_seconds || 0) ? ` · ${Number(payload.elapsed_seconds).toFixed(2)}s` : ""}`;
}

function formatToolEvent(payload: Json): string {
  const status = payload.budget_exhausted === true ? "预算耗尽" : payload.deduplicated === true ? "复用结果" : payload.succeeded === false ? "失败" : "成功";
  const index = Number(payload.tool_call_index || 0), limit = Number(payload.tool_call_limit || 0);
  return `${status} · ${text(payload.tool_name) || "工具调用"}${index && limit ? ` ${index}/${limit}` : ""}${Number(payload.elapsed_seconds || 0) ? ` · ${Number(payload.elapsed_seconds).toFixed(2)}s` : ""}${text(payload.error) ? ` · ${text(payload.error)}` : ""}`;
}
function PlanCard({ plan, onConfirm, onCancel, onRevise }: any) { const pending = plan.status === "pending"; const appliedSkills = list(plan.applied_skills); const row = (label: string, value: any) => <section><span>{label}</span>{Array.isArray(value) ? <ul>{value.map((item) => <li key={String(item)}>{text(item.name || item.description || item)}</li>)}</ul> : <p>{text(value) || "未提供"}</p>}</section>; return <article className={pending ? "chapter-plan-card pending" : "chapter-plan-card"}><header><div><span className="plan-kicker">CHAPTER PLAN · V{plan.version || 1}</span><h3>第 {plan.chapter_number} 章{pending ? "候选计划" : "已执行计划"}</h3>{appliedSkills.length > 0 && <div className="plan-skill-row">{appliedSkills.map((item) => <span key={`${text(obj(item).skill_id)}-${text(obj(item).content_hash)}`}>已应用 Skill：{text(obj(item).skill_id)} · {text(obj(item).content_hash).slice(0, 8)}</span>)}</div>}</div><span className="plan-status">{pending ? "等待确认" : text(plan.status) === "confirmed" ? "已执行" : text(plan.status)}</span></header><div className="plan-grid"><section className="plan-wide"><span>本章目标</span><p>{text(plan.goal) || "未提供"}</p></section>{row("地点", plan.location)}{row("当前时间", plan.current_time)}{row("出场人物", list(plan.participants).map((item) => obj(item).name || obj(item).character_id))}{row("必须发生", plan.required_beats)}{row("禁止发生", plan.forbidden_events)}{row("推进伏笔", plan.relevant_hooks)}{row("风格重点", plan.style_focus)}<section className="plan-wide"><span>结尾悬念</span><p>{text(plan.ending_hook) || "未提供"}</p></section></div><footer><span>目标约 {plan.target_words || 0} 字</span></footer>{pending && <div className="plan-actions"><button className="primary-button" onClick={() => onConfirm(plan.proposal_id)}>确认并开始写作</button><button className="secondary-button" onClick={() => onRevise(plan.proposal_id)}>调整计划</button><button className="plan-cancel" onClick={() => onCancel(plan.proposal_id)}>取消</button></div>}</article>; }
function WorkView({ project, chapter, bound, onWrite, onBatch, onChapter, onCloseChapter, onRewrite }: { project: Json | null; chapter: Json | null; bound: boolean; onWrite: () => void; onBatch: () => void; onChapter: (number: number) => Promise<void>; onCloseChapter: () => void; onRewrite: () => void }) {
  if (chapter) return <div className="work-container"><ChapterReader chapter={chapter} onClose={onCloseChapter} onRewrite={onRewrite} /></div>;
  if (!project) return <div className="work-container"><div className="work-header"><div><span className="eyebrow">PROJECT SPACE</span><h1>选择一部作品</h1><p>从左侧“我的作品”选择作品，或创建一部新小说。</p></div></div><div className="work-empty">当前会话没有关联作品。</div></div>;
  const hooks = list(project.hooks), chapters = list(project.chapters), characters = list(project.characters);
  return <div className="work-container"><div className="work-header"><div><span className="eyebrow">PROJECT SPACE</span><h1>{text(project.title)}</h1><p>{text(project.genre)} · 每章约 {text(project.chapter_target_words)} 字</p><p className="work-session-note">{bound ? "当前聊天已绑定此作品。" : "当前仅查看此作品。"}</p></div><div className="work-header-actions"><button className="secondary-button" onClick={onBatch}>连续创作</button><button className="secondary-button" onClick={onWrite}>继续写作</button></div></div><div className="work-content"><div className="metric-grid">{[[ "章节进度", `${project.last_committed_chapter}/${project.target_chapters}` ], ["当前位置", text(project.current_location)], ["当前时间", text(project.current_time)], ["活跃伏笔", hooks.filter((item) => obj(item).status !== "resolved").length]].map(([label, value]) => <div className="metric-card" key={label as string}><span>{label}</span><strong>{value}</strong></div>)}</div><div className="work-columns"><article className="work-card"><div className="card-heading"><h2>章节</h2><span>{chapters.length} 章</span></div><div className="detail-list">{chapters.length ? chapters.map((item) => <button key={text(obj(item).chapter_number)} className="detail-item chapter-list-button" onClick={() => void onChapter(Number(obj(item).chapter_number))}><strong>第 {text(obj(item).chapter_number)} 章 · {text(obj(item).title)}</strong><span>{text(obj(item).word_count)} 字</span><span className="status-pill">{CHAPTER_STATUS[text(obj(item).status)] || text(obj(item).status)}</span></button>) : <div className="empty-list">尚未生成章节</div>}</div></article><article className="work-card"><div className="card-heading"><h2>伏笔</h2><span>当前状态</span></div><div className="detail-list">{hooks.map((item) => <div className="detail-item" key={text(obj(item).hook_id)}><strong>{text(obj(item).display_name || obj(item).name || obj(item).description)}</strong><span>{text(obj(item).description)}</span><span className="status-pill">{HOOK_STATUS[text(obj(item).status)] || text(obj(item).status)}</span></div>)}</div></article></div><article className="work-card"><div className="card-heading"><h2>人物状态</h2><span>最新快照</span></div><div className="character-grid">{characters.map((item) => <div className="character-card" key={text(obj(item).character_id)}><strong>{text(obj(item).name)}</strong><p>{text(obj(item).status)} · {text(obj(item).emotion)}</p><p>{text(obj(item).location)}</p><p>目标：{text(obj(item).goal)}</p></div>)}</div></article></div></div>;
}
function ChapterReader({ chapter, onClose, onRewrite }: any) { return <article className="chapter-reader"><button className="chapter-reader-back" onClick={onClose}>← 返回作品概览</button><header className="chapter-reader-heading"><span className="eyebrow">CHAPTER READER</span><h2>第 {text(chapter.chapter_number)} 章 · {text(chapter.title)}</h2><p>{text(chapter.word_count)} 字</p><button className="secondary-button chapter-rewrite-button" onClick={onRewrite}>重写本章</button></header><div className="chapter-reader-body"><Text value={text(chapter.content)} /></div></article>; }
function CreateDialog({ onClose, onSubmit }: { onClose: () => void; onSubmit: (payload: Json) => void }) { const [data, setData] = useState(DEFAULT_BOOK); const change = (key: string, value: unknown) => setData((old) => ({ ...old, [key]: value })); return <dialog className="create-dialog" open><form onSubmit={(event) => { event.preventDefault(); onSubmit(data); }}><DialogHead kicker="NEW STORY" title="创建小说" onClose={onClose}/><div className="form-grid"><label>小说标题<input value={text(data.title)} onChange={(e) => change("title", e.target.value)} required/></label><label>题材<input value={text(data.genre)} onChange={(e) => change("genre", e.target.value)} required/></label><label className="full">核心创意<textarea value={text(data.premise)} onChange={(e) => change("premise", e.target.value)} required/></label><label>主角<input value={text(data.protagonist)} onChange={(e) => change("protagonist", e.target.value)} required/></label><label>叙事基调<input value={text(data.tone)} onChange={(e) => change("tone", e.target.value)} required/></label><label className="full">核心冲突<textarea value={text(data.central_conflict)} onChange={(e) => change("central_conflict", e.target.value)} required/></label><label>目标章节<input type="number" value={Number(data.target_chapters)} onChange={(e) => change("target_chapters", Number(e.target.value))}/></label><label>每章目标字数<input type="number" value={Number(data.chapter_target_words)} onChange={(e) => change("chapter_target_words", Number(e.target.value))}/></label></div><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button">生成小说基础资料</button></div></form></dialog>; }
function BatchDialog({ onClose, onSubmit }: any) { const [count, setCount] = useState(3), [auto, setAuto] = useState(false), [instruction, setInstruction] = useState(""); return <dialog className="create-dialog batch-dialog" open><form onSubmit={(e) => { e.preventDefault(); onSubmit({ chapter_count: count, auto_confirm: auto, instruction }); }}><DialogHead kicker="SERIAL WRITING" title="连续创作" onClose={onClose}/><p className="dialog-description">每章都会基于最新正史重新规划；strict 审稿未通过、模型异常或计划失败时自动暂停。</p><div className="form-grid"><label>计划章节数<input type="number" min="2" max="12" value={count} onChange={(e) => setCount(Number(e.target.value))}/></label><label className="full checkbox-field"><input type="checkbox" checked={auto} onChange={(e) => setAuto(e.target.checked)}/>自动确认每章计划（无人值守连续创作）<span>仍会执行 Writer、Reviewer、质量门禁和状态分析；任一章节未通过或异常将暂停。</span></label><label className="full">整体创作要求（可选）<textarea value={instruction} onChange={(e) => setInstruction(e.target.value)}/></label></div><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button">开始连续创作</button></div></form></dialog>; }
function RewriteDialog({ project, chapter, onClose, onSubmit }: any) { const [instruction, setInstruction] = useState(""); const number = Number(chapter?.chapter_number || 0); const last = Number(project?.last_committed_chapter || number); return <dialog className="create-dialog rewrite-dialog" open><form onSubmit={(e) => { e.preventDefault(); onSubmit({ chapter_number: number, instruction }); }}><DialogHead kicker="REWRITE CHAPTER" title={`重写第 ${number} 章`} onClose={onClose}/><div className="rewrite-impact"><strong>时间线将发生变化</strong><p>{number === last ? `原第 ${number} 章会退出正史并保留在历史归档中。` : `原第 ${number}～${last} 章都会退出正史并保留在历史归档中。`}</p></div><label className="rewrite-instruction-label">本次重写要求<textarea value={instruction} onChange={(e) => setInstruction(e.target.value)} placeholder="例如：保留人物动机，但延后关键线索出现；减少技术说明，加强人物冲突。"/><span>可以留空。生成候选计划后，你仍然可以在聊天中继续调整。</span></label><p className="rewrite-notice">确认后会立即归档原时间线并生成新计划。之后取消候选计划不会自动恢复旧章节。</p><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button danger-button">确认并生成计划</button></div></form></dialog>; }
function DialogHead({ kicker, title, onClose }: any) { return <div className="dialog-heading"><div><span className="eyebrow">{kicker}</span><h2>{title}</h2></div><button type="button" className="icon-button" onClick={onClose}>×</button></div>; }
function MemoryDrawer({ memories, bookId, onClose, onUpdate, onToggle }: any) { const [scope, setScope] = useState("all"), [status, setStatus] = useState("active"); const [editing, setEditing] = useState<Json | null>(null); const filtered = memories.filter((item: Json) => (status === "all" || text(item.status) === status) && (scope === "all" || scope === text(item.scope_type)) && (scope !== "book" || text(item.scope_id) === bookId)); return <><aside className="side-drawer open"><div className="drawer-heading"><div><span className="eyebrow">MEMORY</span><h2>长期记忆</h2></div><button className="icon-button" onClick={onClose}>×</button></div><p className="drawer-description">这里只保存跨会话偏好和作品指令，不包含小说正史。</p><div className="drawer-filters"><select value={scope} onChange={(e) => setScope(e.target.value)}><option value="all">全部作用域</option><option value="global">全局</option><option value="book">当前作品</option></select><select value={status} onChange={(e) => setStatus(e.target.value)}><option value="active">生效中</option><option value="all">全部状态</option><option value="superseded">已替代</option><option value="disabled">已停用</option></select></div><div className="memory-list">{filtered.length ? filtered.map((item: Json) => <article className={text(item.status) === "active" ? "memory-card" : "memory-card muted-card"} key={text(item.memory_id)}><div className="memory-card-heading"><strong>{text(item.description)}</strong><span>{text(item.status)}</span></div><p>{text(item.content)}</p><div className="memory-meta">{text(item.memory_type)} · {text(item.scope_type)} · 重要性 {text(item.importance)}</div><div className="memory-actions"><button onClick={() => setEditing(item)}>编辑</button><button onClick={() => void onToggle(text(item.memory_id), text(item.status) === "disabled")}>{text(item.status) === "disabled" ? "恢复" : "停用"}</button></div></article>) : <div className="empty-list">当前筛选条件下没有长期记忆</div>}</div></aside>{editing && <MemoryEdit memory={editing} onClose={() => setEditing(null)} onSave={async (value: Json) => { await onUpdate(text(editing.memory_id), value); setEditing(null); }}/>}</>; }
function MemoryEdit({ memory, onClose, onSave }: any) { const [data, setData] = useState(memory); return <dialog className="create-dialog memory-edit-dialog" open><form onSubmit={(e) => { e.preventDefault(); onSave(data); }}><DialogHead kicker="EDIT MEMORY" title="修改记忆" onClose={onClose}/><div className="form-grid"><label>类型<input value={text(data.memory_type)} onChange={(e) => setData({ ...data, memory_type: e.target.value })}/></label><label>重要性<input type="number" value={Number(data.importance)} onChange={(e) => setData({ ...data, importance: Number(e.target.value) })}/></label><label className="full">名称<input value={text(data.name)} onChange={(e) => setData({ ...data, name: e.target.value })}/></label><label className="full">目录摘要<input value={text(data.description)} onChange={(e) => setData({ ...data, description: e.target.value })}/></label><label className="full">完整内容<textarea value={text(data.content)} onChange={(e) => setData({ ...data, content: e.target.value })}/></label></div><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button">保存修改</button></div></form></dialog>; }
function CreativeControlDrawer({ control, onClose, onSave }: any) { const [data, setData] = useState(control); return <aside className="side-drawer open"><form onSubmit={(event) => { event.preventDefault(); void onSave({ author_intent: text(data.author_intent), current_focus: text(data.current_focus), current_focus_mode: text(data.current_focus_mode) || "single_chapter" }); }}><div className="drawer-heading"><div><span className="eyebrow">CREATIVE CONTROL</span><h2>创作控制</h2></div><button type="button" className="icon-button" onClick={onClose}>×</button></div><p className="drawer-description">作者意图长期生效；当前焦点可仅作用于下一次成功提交的章节，或持续到你手动清除。</p><div className="control-fields"><label>作者意图<textarea value={text(data.author_intent)} onChange={(event) => setData({ ...data, author_intent: event.target.value })} placeholder="例如：保持克制叙事，不急于揭露反派；优先人物关系的张力。" /></label><label>当前焦点<textarea value={text(data.current_focus)} onChange={(event) => setData({ ...data, current_focus: event.target.value })} placeholder="例如：下一章推进师父秘密与主角信任危机。" /></label><label className="focus-mode">焦点生命周期<select value={text(data.current_focus_mode) || "single_chapter"} onChange={(event) => setData({ ...data, current_focus_mode: event.target.value })}><option value="single_chapter">仅下一章成功提交前有效</option><option value="persistent">持续生效，直到手动清除</option></select></label></div><div className="dialog-actions"><button type="button" className="secondary-button" onClick={onClose}>取消</button><button className="primary-button">保存控制面</button></div></form></aside>; }
function TraceDrawer({ trace, onClose }: any) { const rows = [["预算", `${trace?.estimated_tokens || 0}/${trace?.budget || 0} Token`], ["受保护来源", trace?.protected_source_ids || []], ["长期记忆", trace?.selected_memory_ids || []], ["压缩来源", trace?.compressed_source_ids || []], ["排除来源", trace?.excluded_source_ids || []], ["说明", trace?.notes || []]]; return <aside className="side-drawer open"><div className="drawer-heading"><div><span className="eyebrow">CONTEXT TRACE</span><h2>本轮上下文</h2></div><button className="icon-button" onClick={onClose}>×</button></div><div className="trace-content">{trace ? rows.map(([label, value]) => <section className="trace-section" key={label as string}><h3>{label}</h3>{Array.isArray(value) ? <ul>{value.length ? value.map((item) => <li key={String(item)}>{String(item)}</li>) : <li>无</li>}</ul> : <p>{String(value)}</p>}</section>) : <div className="empty-list">这条消息没有 Context Trace。</div>}</div></aside>; }
function mergeTimeline(items: TimelineEvent[]) { const map = new Map(items.map((item) => [item.event_id, item])); return [...map.values()].sort((a, b) => a.sequence - b.sequence); }
function errorMessage(error: unknown) { return error instanceof Error ? error.message : "请求失败"; }
