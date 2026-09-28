import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import type { ReactNode } from "react";
import {
  api,
  type ActionProposal,
  type Json,
  type ModelConfig,
  type ReasoningLevel,
  type ProjectSummary,
  type Session,
  type SkillOption,
  type TimelineEvent,
} from "./api";
import { SimulationWorkspace } from "./features/simulation/SimulationWorkspace";
import { ModelSettings } from "./ModelSettings";
import { RoundedSelect } from "./RoundedSelect";

const DEFAULT_BOOK: Json = {
  title: "",
  genre: "",
  premise: "",
  protagonist: "",
  tone: "",
  central_conflict: "",
  target_chapters: 6,
  chapter_target_words: 1200,
  language: "zh",
};
const HOOK_STATUS: Record<string, string> = {
  open: "未解",
  progressing: "推进中",
  resolved: "已回收",
  deferred: "暂缓",
};
const CHAPTER_STATUS: Record<string, string> = {
  ready_for_review: "审查通过，待人工复阅",
  review_warning: "审查通过（有提示）",
  draft_rejected: "审查未通过",
};
const REASONING_LABELS: Record<ReasoningLevel, string> = {
  default: "默认", off: "关闭", low: "低", medium: "中", high: "高", max: "最大",
};
const obj = (value: unknown): Json =>
  value && typeof value === "object" ? (value as Json) : {};
const text = (value: unknown) =>
  typeof value === "string" ? value : value == null ? "" : String(value);
const list = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);
const skillName = (skill: SkillOption | undefined, fallback: string) =>
  skill?.display_name || skill?.name || fallback;

/** 侧栏保持轻量，不额外引入图标库。 */
function NavIcon({ name }: { name: "write" | "batch" | "theater" | "memory" | "control" | "chevron" | "plus" | "close" | "delete" | "settings" }) {
  const paths = {
    chevron: <path d="m9 5 7 7-7 7" />,
    plus: <path d="M12 5v14M5 12h14" />,
    close: <path d="m6 6 12 12M18 6 6 18" />,
    delete: <><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13M10 11v5M14 11v5" /></>,
    write: <><path d="M4 20l4.2-1 9.5-9.5a2.1 2.1 0 0 0-3-3L5.2 16z" /><path d="M13.4 7.6l3 3" /></>,
    batch: <><path d="M4 5.5h6.5v13H4zM13.5 5.5H20v13h-6.5z" /><path d="M7 9h1M16 9h1M7 13h1M16 13h1" /></>,
    theater: <><path d="M4 6h16v12H4z" /><path d="M8 10c1.2 1.5 2.8 1.5 4 0 1.2 1.5 2.8 1.5 4 0M8 15h8" /></>,
    memory: <><circle cx="12" cy="12" r="7" /><path d="M9.5 12h5M12 9.5v5" /></>,
    control: <><path d="M5 7h14M5 17h14" /><circle cx="9" cy="7" r="2" /><circle cx="15" cy="17" r="2" /></>,
    settings: <><path d="M10.6 2.8h2.8l.5 2.1c.5.2 1 .4 1.4.6l1.9-1.1 2 2-1.1 1.9c.3.4.5.9.6 1.4l2.1.5v2.8l-2.1.5c-.2.5-.4 1-.6 1.4l1.1 1.9-2 2-1.9-1.1c-.4.3-.9.5-1.4.6l-.5 2.1h-2.8l-.5-2.1c-.5-.2-1-.4-1.4-.6l-1.9 1.1-2-2 1.1-1.9c-.3-.4-.5-.9-.6-1.4l-2.1-.5v-2.8l2.1-.5c.2-.5.4-1 .6-1.4L4.8 6.4l2-2 1.9 1.1c.4-.3.9-.5 1.4-.6z" /><circle cx="12" cy="12" r="3" /></>,
  } satisfies Record<string, ReactNode>;
  return <svg className="nav-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">{paths[name]}</svg>;
}

export default function App() {
  const [bootstrap, setBootstrap] = useState<{
    model: string;
    sessions: any[];
    projects: ProjectSummary[];
    actions: Record<string, string>;
    skills?: SkillOption[];
  } | null>(null);
  const [session, setSession] = useState<Session | null>(null);
  const [modelConfig, setModelConfig] = useState<ModelConfig | null>(null);
  const [modelSettingsOpen, setModelSettingsOpen] = useState(false);
  const [modelMenuOpen, setModelMenuOpen] = useState(false);
  const [modelMenuPanel, setModelMenuPanel] = useState<"root" | "model" | "reasoning">("root");
  const [timeline, setTimeline] = useState<TimelineEvent[]>([]);
  const [paging, setPaging] = useState({
    has_more: false,
    next_before_sequence: null as number | null,
  });
  const [view, setView] = useState<"chat" | "work" | "simulation">("chat");
  const [viewingBookId, setViewingBookId] = useState<string | null>(null);
  const [project, setProject] = useState<Json | null>(null);
  const [chapter, setChapter] = useState<Json | null>(null);
  const [activeAction, setActiveAction] = useState("chat");
  const [busy, setBusy] = useState(false);
  const [deletingResource, setDeletingResource] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [foundationReview, setFoundationReview] = useState<ActionProposal | null>(null);
  const [booksCollapsed, setBooksCollapsed] = useState(false);
  const [sessionsCollapsed, setSessionsCollapsed] = useState(false);
  const [batchOpen, setBatchOpen] = useState(false);
  const [rewriteOpen, setRewriteOpen] = useState(false);
  const [memoryOpen, setMemoryOpen] = useState(false);
  const [controlOpen, setControlOpen] = useState(false);
  const [creativeControl, setCreativeControl] = useState<Json | null>(null);
  const [traceOpen, setTraceOpen] = useState(false);
  const [memories, setMemories] = useState<Json[]>([]);
  const [trace, setTrace] = useState<Json | null>(null);
  const [toast, setToast] = useState<{
    message: string;
    error: boolean;
  } | null>(null);
  const [jobEvents, setJobEvents] = useState<Record<string, any[]>>({});
  const [input, setInput] = useState("");
  const [skillMenuOpen, setSkillMenuOpen] = useState(false);
  const [skillMenuIndex, setSkillMenuIndex] = useState(0);
  const [selectedSkillIds, setSelectedSkillIds] = useState<string[]>([]);
  const scrollRef = useRef<HTMLDivElement>(null);
  const eventSources = useRef<Map<string, EventSource>>(new Map());
  const modelMenuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!modelMenuOpen) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!modelMenuRef.current?.contains(event.target as Node)) setModelMenuOpen(false);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setModelMenuOpen(false);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [modelMenuOpen]);

  const notify = useCallback((message: string, error = false) => {
    setToast({ message, error });
    window.setTimeout(() => setToast(null), 3200);
  }, []);
  const refreshBootstrap = useCallback(
    async () => { setBootstrap(await api.bootstrap()); setModelConfig(await api.modelConfig()); },
    [],
  );
  const loadSession = useCallback(
    async (id: string, before?: number | null) => {
      const data = await api.getSession(id, before);
      setSession(data);
      setViewingBookId(data.book_id);
      if (before)
        setTimeline((old) => mergeTimeline([...data.timeline, ...old]));
      else setTimeline(data.timeline);
      setPaging(data.paging);
      return data;
    },
    [],
  );
  const createSession = useCallback(
    async (bookId: string | null = null) => {
      const data = await api.createSession(bookId);
      setSession(data);
      setTimeline([]);
      setPaging({ has_more: false, next_before_sequence: null });
      setViewingBookId(bookId);
      setActiveAction("chat");
      await refreshBootstrap();
      return data;
    },
    [refreshBootstrap],
  );

  const deleteSession = useCallback(
    async (sessionId: string, title: string) => {
      if (!window.confirm(`永久删除对话“${title}”及其全部消息？此操作无法恢复。`)) return;
      setDeletingResource(`session:${sessionId}`);
      try {
        await api.deleteSession(sessionId);
        const data = await api.bootstrap();
        setBootstrap(data);
        if (session?.session_id === sessionId) {
          const next = data.sessions[0];
          if (next) await loadSession(next.session_id);
          else await createSession();
        }
        notify("对话已删除");
      } catch (error) {
        notify(errorMessage(error), true);
      } finally {
        setDeletingResource(null);
      }
    },
    [createSession, loadSession, notify, session?.session_id],
  );

  const deleteBook = useCallback(
    async (bookId: string, title: string) => {
      if (!window.confirm(`永久删除作品《${title}》及其全部章节、正史和创作记录？历史对话会保留但解除作品绑定。此操作无法恢复。`)) return;
      setDeletingResource(`book:${bookId}`);
      try {
        const result = await api.deleteBook(bookId);
        setBootstrap(await api.bootstrap());
        if (session?.book_id === bookId) await loadSession(session.session_id);
        if (viewingBookId === bookId) {
          setViewingBookId(null);
          setProject(null);
          setChapter(null);
          setView("chat");
        }
        notify(
          result.unbound_session_ids.length
            ? `作品已删除，${result.unbound_session_ids.length} 个历史对话已解除绑定`
            : "作品已删除",
        );
      } catch (error) {
        notify(errorMessage(error), true);
      } finally {
        setDeletingResource(null);
      }
    },
    [loadSession, notify, session?.book_id, session?.session_id, viewingBookId],
  );

  useEffect(() => {
    void (async () => {
      try {
        await refreshBootstrap();
        const data = await api.bootstrap();
        if (data.sessions[0]) await loadSession(data.sessions[0].session_id);
        else await createSession();
      } catch (error) {
        notify(errorMessage(error), true);
      }
    })();
    return () => eventSources.current.forEach((item) => item.close());
  }, [createSession, loadSession, notify, refreshBootstrap]);
  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [timeline, jobEvents]);

  const currentBookId = viewingBookId || session?.book_id || null;
  const modelChoices = modelConfig?.providers.flatMap((provider) => provider.models.map((model_id) => ({ provider_id: provider.id, model_id }))) || [];
  const requestedModel = session?.selected_model || {
    provider_id: modelConfig?.default_provider || "",
    model_id: modelConfig?.default_model || "",
  };
  const currentModel = modelChoices.find((item) => item.provider_id === requestedModel.provider_id && item.model_id === requestedModel.model_id) || modelChoices[0];
  const currentProvider = modelConfig?.providers.find((item) => item.id === currentModel?.provider_id);
  const reasoningLevels = currentProvider?.reasoning_levels_by_model?.[currentModel?.model_id || ""] || ["default"];
  const currentReasoning = reasoningLevels.includes(session?.reasoning_level || "default")
    ? (session?.reasoning_level || "default") as ReasoningLevel : "default";
  const projectTitle = (id: string | null | undefined) =>
    bootstrap?.projects.find((item) => item.book_id === id)?.title || id || "";
  const sessionHasActivity = timeline.some((event) =>
    [
      "message_added",
      "action_started",
      "action_completed",
      "action_failed",
      "tool_called",
      "tool_result",
    ].includes(event.event_type),
  );

  const loadProject = useCallback(
    async (bookId: string | null) => {
      if (!bookId) {
        setProject(null);
        return;
      }
      try {
        setProject(await api.getProject(bookId));
      } catch (error) {
        notify(errorMessage(error), true);
      }
    },
    [notify],
  );
  useEffect(() => {
    if (view === "work") void loadProject(currentBookId);
  }, [view, currentBookId, loadProject]);
  const openCreativeControl = async () => {
    if (!currentBookId) {
      notify("请先选择一部作品", true);
      return;
    }
    try {
      setCreativeControl(await api.getCreativeControl(currentBookId));
      setControlOpen(true);
    } catch (error) {
      notify(errorMessage(error), true);
    }
  };

  const watchJob = useCallback(
    (jobId: string, sessionId: string) => {
      eventSources.current.get(jobId)?.close();
      const source = api.streamJob(jobId, (event) => {
        setJobEvents((old) => ({
          ...old,
          [jobId]: [...(old[jobId] || []), event],
        }));
        if (
          [
            "job_succeeded",
            "job_failed",
            "job_paused",
            "job_interrupted",
            "job_cancelled",
          ].includes(event.event_type)
        ) {
          // 给浏览器至少一帧时间绘制最后一个 preview_delta；否则网络事件很密集
          // 时 React 会把增量与终态合并，用户视觉上就只会看到最终全文。
          window.setTimeout(() => {
            source.close();
            eventSources.current.delete(jobId);
            if (
              event.event_type === "job_succeeded" &&
              typeof obj(event.payload.result).job_id === "string"
            ) {
              watchJob(text(obj(event.payload.result).job_id), sessionId);
            } else {
              setBusy(false);
              // 先读取持久化后的助手交付，再撤掉内存预览。反过来做会在
              // “流式文本 → 正式全文”之间留下一个可见的空白闪烁。
              void loadSession(sessionId)
                .then(() => {
                  setJobEvents((old) => ({
                    ...old,
                    [jobId]: (old[jobId] || []).filter(
                      (item) => !String(item.event_type || "").startsWith("preview_"),
                    ),
                  }));
                })
                .then(() => refreshBootstrap())
                .then(() => loadProject(currentBookId))
                .catch((error) => notify(errorMessage(error), true));
            }
          }, 80);
        }
      });
      eventSources.current.set(jobId, source);
      void api.getJob(jobId).then((item) => {
        if (["failed", "paused", "interrupted", "cancelled"].includes(item.status))
          notify(item.error || "任务失败", true);
      });
    },
    [currentBookId, loadProject, loadSession, notify, refreshBootstrap],
  );

  const send = useCallback(
    async (action = activeAction, content = input, payload: Json = {}) => {
      if (!session || busy) return;
      const normalized = content.trim() || bootstrap?.actions[action] || action;
      if (!normalized && action === "chat") return;
      setBusy(true);
      setInput("");
      const creativePayload =
        action === "chat" ||
        ["create_novel", "write_next", "rewrite_chapter", "start_chapter_batch"].includes(action)
          ? {
              ...payload,
              skill_ids: Array.from(
                new Set([
                  ...list(payload.skill_ids).map((item) => text(item)),
                  ...selectedSkillIds,
                ]),
              ),
            }
          : payload;
      try {
        const result = await api.send(session.session_id, {
          content: normalized,
          action,
          book_id: session.book_id,
          payload: creativePayload,
        });
        // HTTP 只确认 Job 已入队。先在本地补一对时间线事件，让用户不必等
        // Planner 完整结束才看到“写下一章”及其实时模型/工具进度。
        const maximum = timeline.reduce(
          (value, item) => Math.max(value, item.sequence),
          0,
        );
        const now = new Date().toISOString();
        const label = bootstrap?.actions[action] || action;
        setTimeline((old) =>
          mergeTimeline([
            ...old,
            {
              event_id: `local-message-${result.job_id}`,
              sequence: maximum + 1,
              event_type: "message_added",
              created_at: now,
              payload: {
                role: "user",
                content: normalized,
                action,
                metadata: {},
              },
            },
            {
              event_id: `local-action-${result.job_id}`,
              sequence: maximum + 2,
              event_type: "action_started",
              created_at: now,
              payload: {
                run_id: result.job_id,
                root_run_id: result.job_id,
                action,
                label,
              },
            },
          ]),
        );
        watchJob(result.job_id, session.session_id);
        setSelectedSkillIds([]);
        if (action !== "revise_chapter_plan") setActiveAction("chat");
      } catch (error) {
        setBusy(false);
        notify(errorMessage(error), true);
      }
    },
    [
      activeAction,
      bootstrap?.actions,
      busy,
      input,
      notify,
      session,
      selectedSkillIds,
      timeline,
      watchJob,
    ],
  );

  const bindBook = async (bookId: string | null): Promise<boolean> => {
    if (!session) return false;
    if (session.book_id === bookId) return true;
    try {
      // 建书讨论本身就是作品上下文的一部分：未绑定会话可原地绑定。
      // 只有已绑定另一部作品时，才隔离会话，避免两本书的上下文混在一起。
      if (sessionHasActivity && session.book_id && bookId && session.book_id !== bookId) {
        if (
          !window.confirm(
            `当前对话已绑定《${projectTitle(session.book_id)}》。\n\n是否创建一个绑定《${projectTitle(bookId)}》的新对话？`,
          )
        )
          return false;
        await createSession(bookId);
        return true;
      } else {
        const data = await api.bindBook(session.session_id, bookId);
        setSession((old) => (old ? { ...old, ...data } : data));
        setViewingBookId(bookId);
        await refreshBootstrap();
        return true;
      }
    } catch (error) {
      notify(errorMessage(error), true);
      return false;
    }
  };
  const choosePreset = (preset: string) => {
    if (preset === "create") {
      setCreateOpen(true);
      return;
    }
    if (preset === "start_chapter_batch") {
      setBatchOpen(true);
      return;
    }
    if (preset === "project_status") {
      setView("work");
      return;
    }
    setActiveAction(preset);
    document.querySelector<HTMLTextAreaElement>("#message-input")?.focus();
  };
  const ensureChatForBook = async () => {
    if (!currentBookId) {
      notify("请先选择一部作品", true);
      return false;
    }
    if (session?.book_id !== currentBookId) return bindBook(currentBookId);
    return true;
  };
  const openMemories = async () => {
    setMemoryOpen(true);
    try {
      setMemories((await api.memories()).memories);
    } catch (error) {
      notify(errorMessage(error), true);
    }
  };
  const openTrace = async () => {
    if (!session) return;
    setTraceOpen(true);
    setTrace((await api.getTrace(session.session_id)).trace);
  };
  const pendingPlan = useMemo(
    () =>
      [...timeline]
        .reverse()
        .find(
          (event) =>
            event.event_type.startsWith("chapter_plan_") &&
            text(event.payload.status) === "pending",
        )?.payload ?? null,
    [timeline],
  );
  const renderedTimeline = useMemo(() => {
    const items = mergeTimeline(timeline);
    const terminalRuns = new Set(
      items
        .filter((item) => ["action_completed", "action_failed"].includes(item.event_type))
        .map((item) => text(item.payload.run_id))
        .filter(Boolean),
    );
    const latestPendingProposal = new Map<string, number>();
    const closedProposalIds = new Set<string>();
    for (const item of items) {
      if (item.event_type === "action_proposal_pending") {
        latestPendingProposal.set(text(item.payload.action_proposal_id), item.sequence);
      }
      if (["action_proposal_confirmed", "action_proposal_cancelled"].includes(item.event_type)) {
        closedProposalIds.add(text(item.payload.action_proposal_id));
      }
    }
    return items.filter(
      (item) =>
        (item.event_type !== "action_started" ||
          !terminalRuns.has(text(item.payload.run_id))) &&
        (item.event_type !== "action_proposal_pending" ||
          (!closedProposalIds.has(text(item.payload.action_proposal_id)) &&
            latestPendingProposal.get(text(item.payload.action_proposal_id)) === item.sequence)),
    );
  }, [timeline]);
  const visibleSkills = useMemo(
    () =>
      (bootstrap?.skills || []).filter(
        (item) =>
          input.trim() === "/" ||
          item.id.startsWith(input.trim().slice(1).toLowerCase()),
      ),
    [bootstrap?.skills, input],
  );
  const selectSkill = (skillId: string) => {
    setSelectedSkillIds((old) =>
      old.includes(skillId) ? old : [...old, skillId],
    );
    if (/^\/[^\s]*$/.test(input.trim())) setInput("");
    setSkillMenuOpen(false);
    setSkillMenuIndex(0);
    window.setTimeout(
      () =>
        document.querySelector<HTMLTextAreaElement>("#message-input")?.focus(),
      0,
    );
  };

  return (
    <div className="app-shell">
      <aside className="sidebar" id="sidebar">
        <div className="brand-row">
          <div>
            <div className="brand-name">StoryWeaver</div>
            <div className="brand-subtitle">小说工作台</div>
          </div>
          <button className="icon-button sidebar-close" aria-label="关闭侧栏">
            <NavIcon name="close" />
          </button>
        </div>
        <nav className="sidebar-nav">
          <section className="nav-group">
            <div className="section-heading">创作</div>
            <button
              className="nav-item"
              onClick={() => choosePreset("write_next")}
            >
              <span className="nav-icon"><NavIcon name="write" /></span>
              <span>写下一章</span>
            </button>
            <button
              className="nav-item"
              onClick={() => choosePreset("start_chapter_batch")}
            >
              <span className="nav-icon"><NavIcon name="batch" /></span>
              <span>连续创作</span>
            </button>
            <button className="nav-item" onClick={() => setView("simulation")}>
              <span className="nav-icon"><NavIcon name="theater" /></span>
              <span>角色剧场</span>
            </button>
          </section>
          <section className="nav-group">
            <div className="section-heading">资料</div>
            <button className="nav-item" onClick={() => void openMemories()}>
              <span className="nav-icon"><NavIcon name="memory" /></span>
              <span>会话记忆</span>
            </button>
            <button
              className="nav-item"
              onClick={() => void openCreativeControl()}
            >
              <span className="nav-icon"><NavIcon name="control" /></span>
              <span>创作控制</span>
            </button>
          </section>
        </nav>
        <section className="sidebar-section">
          <div className="section-heading section-heading-row">
            <button
              className="section-toggle"
              aria-expanded={!booksCollapsed}
              aria-controls="sidebar-books"
              onClick={() => setBooksCollapsed((value) => !value)}
            >
              <span className="section-chevron"><NavIcon name="chevron" /></span>
              我的作品
            </button>
            <button
              className="section-add-button"
              aria-label="创建作品"
              onClick={() => setCreateOpen(true)}
            >
              <NavIcon name="plus" />
            </button>
          </div>
          <div id="sidebar-books" className="compact-list" hidden={booksCollapsed}>
            {bootstrap?.projects.length ? (
              bootstrap.projects.map((item) => (
                <div className="compact-item-shell" key={item.book_id}>
                  <button
                    className={
                      currentBookId === item.book_id
                        ? "compact-item active"
                        : "compact-item"
                    }
                    onClick={() => {
                      setViewingBookId(item.book_id);
                      setView("work");
                    }}
                  >
                    <strong>{item.title}</strong>
                    <span>
                      {item.genre} · {item.target_chapters} 章
                    </span>
                  </button>
                  <button
                    className="compact-delete-button"
                    aria-label={`删除作品《${item.title}》`}
                    title="删除作品"
                    disabled={deletingResource !== null}
                    onClick={() => void deleteBook(item.book_id, item.title)}
                  >
                    <NavIcon name="delete" />
                  </button>
                </div>
              ))
            ) : (
              <div className="empty-list">还没有作品</div>
            )}
          </div>
        </section>
        <section className="sidebar-section conversations-section">
          <div className="section-heading section-heading-row">
            <button
              className="section-toggle"
              aria-expanded={!sessionsCollapsed}
              aria-controls="sidebar-sessions"
              onClick={() => setSessionsCollapsed((value) => !value)}
            >
              <span className="section-chevron"><NavIcon name="chevron" /></span>
              最近对话
            </button>
            <button
              className="section-add-button"
              aria-label="新建对话"
              title="新建对话"
              onClick={() => {
                void createSession().then(() => {
                  setSessionsCollapsed(false);
                  setView("chat");
                }).catch((error) => notify(errorMessage(error), true));
              }}
            >
              <NavIcon name="plus" />
            </button>
          </div>
          <div id="sidebar-sessions" className="compact-list" hidden={sessionsCollapsed}>
            {bootstrap?.sessions.length ? (
              bootstrap.sessions.map((item) => (
                <div className="compact-item-shell" key={item.session_id}>
                  <button
                    className={
                      session?.session_id === item.session_id
                        ? "compact-item active"
                        : "compact-item"
                    }
                    onClick={() => void loadSession(item.session_id)}
                  >
                    <strong>{item.title}</strong>
                    <span>
                      {item.message_count} 条消息 ·{" "}
                      {item.book_id
                        ? `《${projectTitle(item.book_id)}》`
                        : "未绑定作品"}
                    </span>
                  </button>
                  <button
                    className="compact-delete-button"
                    aria-label={`删除对话“${item.title}”`}
                    title="删除对话"
                    disabled={deletingResource !== null}
                    onClick={() => void deleteSession(item.session_id, item.title)}
                  >
                    <NavIcon name="delete" />
                  </button>
                </div>
              ))
            ) : (
              <div className="empty-list">还没有历史对话</div>
            )}
          </div>
        </section>
        <div className="sidebar-footer">
          <button type="button" className="sidebar-settings-entry" onClick={() => setModelSettingsOpen(true)}><NavIcon name="settings" /><span>设置</span></button>
        </div>
      </aside>
      <main className="main-panel">
        <header className="topbar">
          <button className="icon-button mobile-menu">☰</button>
          <div className="view-switcher">
            <button
              className={view === "chat" ? "view-tab active" : "view-tab"}
              onClick={() => setView("chat")}
            >
              聊天
            </button>
            <button
              className={view === "work" ? "view-tab active" : "view-tab"}
              onClick={() => setView("work")}
            >
              作品
            </button>
            <button
              className={view === "simulation" ? "view-tab active" : "view-tab"}
              onClick={() => setView("simulation")}
            >
              模拟
            </button>
          </div>
          <div className="topbar-actions">
            <label className="project-picker-label">当前会话绑定</label>
            <div className="project-picker">
              <RoundedSelect
                label="当前会话绑定"
                value={session?.book_id || ""}
                onChange={(value) => void bindBook(value || null)}
                options={[
                  { value: "", label: "未关联作品" },
                  ...(bootstrap?.projects || []).map((item) => ({ value: item.book_id, label: item.title })),
                ]}
              />
            </div>
          </div>
        </header>
        <section
          className={
            view === "chat"
              ? "view-panel chat-view active"
              : "view-panel chat-view"
          }
        >
          <div className="message-scroll" ref={scrollRef}>
            {!timeline.length && (
              <div className="empty-state">
                <h1>准备好了，随时开始</h1>
                <p>讨论灵感、创建小说，或者继续当前作品。</p>
                <div className="starter-grid">
                  <button onClick={() => setCreateOpen(true)}>
                    <strong>创建小说</strong>
                    <span>从创作简报生成世界、角色和大纲</span>
                  </button>
                  <button onClick={() => choosePreset("write_next")}>
                    <strong>写下一章</strong>
                    <span>点击这里，或直接输入“写下一章”</span>
                  </button>
                  <button onClick={() => setView("work")}>
                    <strong>查看状态</strong>
                    <span>检查人物、事实、伏笔和章节进度</span>
                  </button>
                  <button onClick={() => setActiveAction("chat")}>
                    <strong>讨论灵感</strong>
                    <span>和编辑助手聊人物、冲突与写作方向</span>
                  </button>
                </div>
              </div>
            )}
            {paging.has_more && (
              <div className="load-more-wrap">
                <button
                  className="secondary-button"
                  onClick={() =>
                    session &&
                    void loadSession(
                      session.session_id,
                      paging.next_before_sequence,
                    )
                  }
                >
                  加载更早记录
                </button>
              </div>
            )}
            <div className="message-list">
              {renderedTimeline.map((event) => (
                <TimelineCard
                  key={event.event_id}
                  event={event}
                  actions={bootstrap?.actions || {}}
                  progress={jobEvents[text(event.payload.run_id)] || []}
                  onConfirm={(id, status) =>
                    void send(
                      status === "approved" ? "write_from_plan" : "approve_chapter_plan",
                      status === "approved" ? "根据已批准计划生成本章" : "批准候选章节计划，暂不生成正文",
                      { proposal_id: id },
                    )
                  }
                  onCancel={(id) =>
                    void send("cancel_chapter_plan", "取消候选章节计划", {
                      proposal_id: id,
                    })
                  }
                  onRevise={(id) => {
                    setActiveAction("revise_chapter_plan");
                    setInput("");
                    (window as any).__proposalId = id;
                  }}
                  onConfirmAction={(id, actionType, version) =>
                    session &&
                    void api
                      .confirmActionProposal(id, version)
                      .then((item) => {
                        // 自然语言确认与快捷按钮走同一套 Job/SSE，但此前漏了
                        // 本地 action_started 投影，导致 Writer 的流式预览没有
                        // 可挂载的时间线节点。
                        const maximum = timeline.reduce(
                          (value, entry) => Math.max(value, entry.sequence),
                          0,
                        );
                        setTimeline((old) =>
                          mergeTimeline([
                            ...old,
                            {
                              event_id: `local-action-${item.job_id}`,
                              sequence: maximum + 1,
                              event_type: "action_started",
                              created_at: new Date().toISOString(),
                              payload: {
                                run_id: item.job_id,
                                root_run_id: item.job_id,
                                action: actionType,
                                label: displayActionLabel(actionType, "", bootstrap?.actions || {}),
                              },
                            },
                          ]),
                        );
                        setBusy(true);
                        watchJob(item.job_id, session.session_id);
                      })
                      .catch((error) => notify(errorMessage(error), true))
                  }
                  onCancelAction={(id) =>
                    void api
                      .cancelActionProposal(id)
                      .then(() => session && loadSession(session.session_id))
                      .catch((error) => notify(errorMessage(error), true))
                  }
                  onViewFoundation={(proposal) => setFoundationReview(proposal)}
                  onRegenerateFoundation={(proposal) =>
                    session &&
                    void api
                      .regenerateFoundationProposal(text(proposal.action_proposal_id))
                      .then((item) => {
                        setBusy(true);
                        watchJob(item.job_id, session.session_id);
                      })
                      .catch((error) => notify(errorMessage(error), true))
                  }
                  onRetry={(id) =>
                    session &&
                    void api
                      .retryAction(session.session_id, id)
                      .then((item) => watchJob(item.job_id, session.session_id))
                  }
                  onTrace={() => void openTrace()}
                />
              ))}
            </div>
          </div>
          <div className="composer-wrap">
            {activeAction !== "chat" && (
              <div className="active-action">
                <span>{bootstrap?.actions[activeAction] || activeAction}</span>
                <button onClick={() => setActiveAction("chat")}>×</button>
              </div>
            )}
            <div className="composer-command-wrap">
              {selectedSkillIds.length > 0 && (
                <div className="selected-skill-row" aria-label="已选择创作 Skill">
                  {selectedSkillIds.map((skillId) => (
                    <span key={skillId}>
                      {skillName(
                        bootstrap?.skills?.find((item) => item.id === skillId),
                        skillId,
                      )}
                      <button
                        type="button"
                        aria-label={`移除 ${skillId}`}
                        onClick={() =>
                          setSelectedSkillIds((old) =>
                            old.filter((item) => item !== skillId),
                          )
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
              {skillMenuOpen && (
                <div
                  className="skill-command-menu"
                  role="listbox"
                  aria-label="可用创作 Skill"
                >
                  {visibleSkills.length ? (
                    visibleSkills.map((skill, index) => (
                      <button
                        type="button"
                        role="option"
                        aria-selected={index === skillMenuIndex}
                        className={index === skillMenuIndex ? "active" : ""}
                        key={skill.id}
                        onMouseDown={(event) => {
                          event.preventDefault();
                          selectSkill(skill.id);
                        }}
                      >
                        <span className="skill-command-title">
                          <strong>{skillName(skill, skill.id)}</strong>
                          <code>/{skill.id}</code>
                        </span>
                        <span className="skill-command-summary">
                          {skill.short_description || skill.description}
                        </span>
                        {index === skillMenuIndex &&
                          skill.description !== skill.short_description && (
                            <span className="skill-command-detail">
                              {skill.description}
                            </span>
                          )}
                      </button>
                    ))
                  ) : (
                    <div className="skill-command-empty">
                      没有匹配的创作 Skill
                    </div>
                  )}
                </div>
              )}
              <form
                className="composer"
                onSubmit={(event) => {
                  event.preventDefault();
                  const payload =
                    activeAction === "revise_chapter_plan"
                      ? {
                          proposal_id:
                            (window as any).__proposalId ||
                            pendingPlan?.proposal_id,
                        }
                      : {};
                  void send(activeAction, input, payload);
                }}
              >
                <textarea
                  id="message-input"
                  rows={1}
                  value={input}
                  onChange={(event) => {
                    const value = event.target.value;
                    setInput(value);
                    setSkillMenuOpen(/^\/[^\s]*$/.test(value));
                    setSkillMenuIndex(0);
                  }}
                  onBlur={() =>
                    window.setTimeout(() => setSkillMenuOpen(false), 120)
                  }
                  onKeyDown={(event) => {
                    if (skillMenuOpen && visibleSkills.length) {
                      if (event.key === "ArrowDown") {
                        event.preventDefault();
                        setSkillMenuIndex(
                          (index) => (index + 1) % visibleSkills.length,
                        );
                        return;
                      }
                      if (event.key === "ArrowUp") {
                        event.preventDefault();
                        setSkillMenuIndex(
                          (index) =>
                            (index - 1 + visibleSkills.length) %
                            visibleSkills.length,
                        );
                        return;
                      }
                      if (
                        (event.key === "Enter" || event.key === "Tab") &&
                        !event.shiftKey
                      ) {
                        event.preventDefault();
                        selectSkill(visibleSkills[skillMenuIndex].id);
                        return;
                      }
                      if (event.key === "Escape") {
                        event.preventDefault();
                        setSkillMenuOpen(false);
                        return;
                      }
                    }
                    if (event.key === "Enter" && !event.shiftKey) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }}
                  placeholder={
                    activeAction === "write_next"
                      ? "可以补充本章要求；直接发送则按当前计划继续……"
                      : activeAction === "revise_chapter_plan"
                        ? "告诉 StoryWeaver 希望如何调整本章计划……"
                        : "输入消息，和 StoryWeaver 一起创作……"
                  }
                />
                <div className="composer-footer">
                  <div className="composer-tools">
                    <button
                      type="button"
                      className="tool-button"
                      onClick={() => setCreateOpen(true)}
                    >
                      ＋
                    </button>
                  </div>
                  <div className="composer-actions">
                    {session && (modelChoices.length ? <div className="composer-model-control" ref={modelMenuRef}>
                      {modelMenuOpen && <div className="composer-model-popover">
                        {modelMenuPanel === "root" && <>
                          <button type="button" className="composer-model-menu-row" onClick={() => setModelMenuPanel("model")}><span>模型</span><strong>{currentModel.model_id}</strong><span className="composer-model-chevron">›</span></button>
                          <button type="button" className="composer-model-menu-row" disabled={reasoningLevels.length < 2} onClick={() => setModelMenuPanel("reasoning")}><span>推理等级</span><strong>{REASONING_LABELS[currentReasoning]}</strong><span className="composer-model-chevron">›</span></button>
                        </>}
                        {modelMenuPanel === "model" && <>
                          <button type="button" className="composer-model-menu-back" onClick={() => setModelMenuPanel("root")}>‹ <span>模型</span></button>
                          <div className="composer-model-options">
                            {modelConfig?.providers.map((provider) => <div key={provider.id}>
                              <div className="composer-model-group">{provider.name}</div>
                              {provider.models.map((model) => <button type="button" className="composer-model-option" key={model} aria-current={currentModel.provider_id === provider.id && currentModel.model_id === model ? "true" : undefined} onClick={() => {
                                void api.selectSessionModel(session.session_id, { provider_id: provider.id, model_id: model }).then((selected_model) => {
                                  setSession((old) => old && old.session_id === session.session_id ? { ...old, selected_model, reasoning_level: "default" } : old);
                                  setModelMenuOpen(false);
                                }).catch((error) => notify(errorMessage(error), true));
                              }}>{model}{currentModel.provider_id === provider.id && currentModel.model_id === model && <span>✓</span>}</button>)}
                            </div>)}
                          </div>
                        </>}
                        {modelMenuPanel === "reasoning" && <>
                          <button type="button" className="composer-model-menu-back" onClick={() => setModelMenuPanel("root")}>‹ <span>推理等级</span></button>
                          <div className="composer-model-options">
                            {reasoningLevels.map((level) => <button type="button" className="composer-model-option" key={level} aria-current={currentReasoning === level ? "true" : undefined} onClick={() => {
                              void api.selectSessionReasoning(session.session_id, level as ReasoningLevel).then(() => {
                                setSession((old) => old && old.session_id === session.session_id ? { ...old, reasoning_level: level as ReasoningLevel } : old);
                                setModelMenuOpen(false);
                              }).catch((error) => notify(errorMessage(error), true));
                            }}>{REASONING_LABELS[level as ReasoningLevel]}{currentReasoning === level && <span>✓</span>}</button>)}
                          </div>
                        </>}
                      </div>}
                      <button type="button" className="composer-model-trigger" aria-label={`模型 ${currentModel.model_id}，推理等级 ${REASONING_LABELS[currentReasoning]}`} aria-expanded={modelMenuOpen} onClick={() => {
                        setModelMenuPanel("root");
                        setModelMenuOpen((open) => !open);
                      }}><span>{currentModel.model_id}</span><small>{reasoningLevels.length > 1 ? REASONING_LABELS[currentReasoning] : ""}</small><span className="composer-model-chevron">›</span></button>
                    </div> : <button type="button" className="session-model-setup" onClick={() => setModelSettingsOpen(true)}>配置模型</button>)}
                    <button type="submit" className="send-button" disabled={busy || (!!modelConfig && modelChoices.length === 0)} title={modelConfig && !modelChoices.length ? "请先配置模型" : "发送消息"}>↑</button>
                  </div>
                </div>
              </form>
            </div>
            <p className="safety-note">
              生成内容可能存在偏差，重要设定请检查后再继续写作。
            </p>
          </div>
        </section>
        <section
          className={
            view === "work"
              ? "view-panel work-view active"
              : "view-panel work-view"
          }
        >
          <WorkView
            project={project}
            chapter={chapter}
            bound={session?.book_id === currentBookId}
            onWrite={async () => {
              if (await ensureChatForBook()) {
                setView("chat");
                choosePreset("write_next");
              }
            }}
            onBatch={() => setBatchOpen(true)}
            onChapter={async (number) => {
              if (currentBookId)
                setChapter(await api.getChapter(currentBookId, number));
            }}
            onCloseChapter={() => setChapter(null)}
            onRewrite={() => setRewriteOpen(true)}
            onReviseFoundation={(scope) => {
              if (!currentBookId || !session) return;
              void api.createFoundationRevision(currentBookId, session.session_id, scope)
                .then((item) => setFoundationReview(item.proposal))
                .catch((error) => notify(errorMessage(error), true));
            }}
          />
        </section>
        <section
          className={
            view === "simulation"
              ? "view-panel simulation-view active"
              : "view-panel simulation-view"
          }
        >
          <SimulationWorkspace
            bookId={currentBookId}
            onError={(message) => notify(message, true)}
          />
        </section>
      </main>
      {modelSettingsOpen && <ModelSettings onClose={() => setModelSettingsOpen(false)} onChanged={() => { void refreshBootstrap(); }} />}
      {createOpen && (
        <CreateDialog
          skills={bootstrap?.skills || []}
          onClose={() => setCreateOpen(false)}
          onSubmit={(payload: Json) => {
            setCreateOpen(false);
            void (async () => {
              if (sessionHasActivity || session?.book_id) await createSession();
              await send(
                "create_novel",
                `创建小说《${text(payload.title)}》`,
                payload,
              );
            })();
          }}
        />
      )}
      {foundationReview && (
        <FoundationEditor
          key={`${text(foundationReview.action_proposal_id)}:${text(obj(foundationReview.payload).version)}`}
          proposal={foundationReview}
          onClose={() => setFoundationReview(null)}
          onSaved={(proposal) => {
            setFoundationReview(proposal);
            if (session) void loadSession(session.session_id);
          }}
          onConfirm={(proposalId, version) =>
            session &&
            void api
              .confirmActionProposal(proposalId, version)
              .then((item) => {
                setFoundationReview(null);
                setBusy(true);
                watchJob(item.job_id, session.session_id);
              })
              .catch((error) => notify(errorMessage(error), true))
          }
          onError={(message) => notify(message, true)}
        />
      )}
      {batchOpen && (
        <BatchDialog
          onClose={() => setBatchOpen(false)}
          onSubmit={(payload: Json) => {
            setBatchOpen(false);
            void (async () => {
              if (await ensureChatForBook()) {
                setView("chat");
                await send(
                  "start_chapter_batch",
                  `连续创作 ${payload.chapter_count} 章`,
                  payload,
                );
              }
            })();
          }}
        />
      )}
      {rewriteOpen && (
        <RewriteDialog
          project={project}
          chapter={chapter}
          onClose={() => setRewriteOpen(false)}
          onSubmit={(payload: Json) => {
            setRewriteOpen(false);
            setView("chat");
            void send(
              "rewrite_chapter",
              `重写第${payload.chapter_number}章${payload.instruction ? `：${payload.instruction}` : ""}`,
              payload,
            );
          }}
        />
      )}
      <div
        className={
          memoryOpen || traceOpen || controlOpen
            ? "drawer-backdrop show"
            : "drawer-backdrop"
        }
        onClick={() => {
          setMemoryOpen(false);
          setTraceOpen(false);
          setControlOpen(false);
        }}
      ></div>
      {memoryOpen && (
        <MemoryDrawer
          memories={memories}
          bookId={session?.book_id || null}
          onClose={() => setMemoryOpen(false)}
          onToggle={async (id: string, restore: boolean) => {
            const result = restore
              ? await api.restoreMemory(id)
              : await api.disableMemory(id);
            setMemories((old) =>
              old.map((item) =>
                text(item.memory_id) === id ? result.memory : item,
              ),
            );
          }}
          onDelete={async (id: string) => {
            if (!window.confirm("永久删除这条会话记忆？此操作无法恢复。")) return;
            await api.deleteMemory(id);
            setMemories((old) => old.filter((item) => text(item.memory_id) !== id));
          }}
          onCorrect={async (id: string, body: Json) => {
            const result = await api.correctMemory(id, body);
            setMemories((old) => [
              result.memory,
              ...old.map((item) =>
                text(item.memory_id) === result.replaced_memory_id
                  ? { ...item, status: "superseded" }
                  : item,
              ),
            ]);
          }}
        />
      )}
      {traceOpen && (
        <TraceDrawer trace={trace} onClose={() => setTraceOpen(false)} />
      )}
      {controlOpen && creativeControl && (
        <CreativeControlDrawer
          control={creativeControl}
          onClose={() => setControlOpen(false)}
          onSave={async (value: Json) => {
            if (!currentBookId) return;
            const updated = await api.updateCreativeControl(
              currentBookId,
              value,
            );
            setCreativeControl(updated);
            setProject((old) =>
              old ? { ...old, creative_control: updated } : old,
            );
            notify("创作控制已保存，将从下一次规划或写作开始生效。");
          }}
        />
      )}
      {toast && (
        <div className={toast.error ? "toast error show" : "toast show"}>
          {toast.message}
        </div>
      )}
    </div>
  );
}

function TimelineCard({
  event,
  actions,
  progress,
  onConfirm,
  onCancel,
  onRevise,
  onConfirmAction,
  onCancelAction,
  onViewFoundation,
  onRegenerateFoundation,
  onRetry,
  onTrace,
}: {
  event: TimelineEvent;
  actions: Record<string, string>;
  progress: any[];
  onConfirm: (id: string, status: string) => void;
  onCancel: (id: string) => void;
  onRevise: (id: string) => void;
  onConfirmAction: (id: string, actionType: string, version?: number) => void;
  onCancelAction: (id: string) => void;
  onViewFoundation: (proposal: ActionProposal) => void;
  onRegenerateFoundation: (proposal: ActionProposal) => void;
  onRetry: (id: string) => void;
  onTrace: () => void;
}) {
  const payload = event.payload || {};
  const [processOpen, setProcessOpen] = useState(event.event_type === "action_started");
  if (event.event_type === "message_added") {
    const metadata = obj(payload.metadata);
    const chapterResult = obj(metadata.chapter_result);
    const skillResolution = obj(obj(metadata.context_trace).skill_resolution);
    const materializedSkills = list(skillResolution.materialized_ids).map(text);
    const metadataOnlySkills = list(skillResolution.metadata_only_ids).map(text);
    return (
      <article
        className={
          payload.role === "user" ? "message user" : "message assistant"
        }
      >
        <div className="message-content">
          {payload.role !== "user" && Object.keys(chapterResult).length > 0 ? (
            <ChapterCompletionCard result={chapterResult} />
          ) : (
            <Text value={text(payload.content)} />
          )}
          {payload.role !== "user" && materializedSkills.length > 0 && (
            <div className="plan-skill-row">
              {materializedSkills.map((skillId) => (
                <span key={skillId}>已完整加载 Skill：{skillId}</span>
              ))}
            </div>
          )}
          {payload.role !== "user" && metadataOnlySkills.length > 0 && (
            <div className="plan-skill-row">
              {metadataOnlySkills.map((skillId) => (
                <span key={skillId}>仅加载 Skill metadata：{skillId}</span>
              ))}
            </div>
          )}
          {payload.role !== "user" &&
            Boolean(metadata.context_trace) && (
              <div className="message-meta">
                <button className="trace-link" onClick={onTrace}>
                  查看本轮上下文
                </button>
              </div>
            )}
        </div>
      </article>
    );
  }
  if (event.event_type === "book_bound")
    return (
      <div className="binding-divider">
        <span>已切换至作品</span>
      </div>
    );
  if (event.event_type === "story_timeline_rewritten")
    return (
      <div className="binding-divider">
        <span>时间线已归档，当前章节重新开始</span>
      </div>
    );
  if (
    event.event_type.startsWith("chapter_plan_") &&
    !["cancelled", "expired"].includes(text(payload.status))
  )
    return (
      <PlanCard
        plan={payload}
        onConfirm={onConfirm}
        onCancel={onCancel}
        onRevise={onRevise}
      />
    );
  // 故事设定/大纲修订由全屏编辑器负责保存与确认，不在聊天时间线重复渲染通用确认卡。
  if (event.event_type === "action_proposal_pending" && text(payload.action_type) === "apply_foundation_revision")
    return null;
  if (event.event_type === "action_proposal_pending")
    return (
      <ActionProposalCard
        proposal={payload as unknown as ActionProposal}
        onConfirm={onConfirmAction}
        onCancel={onCancelAction}
        onView={onViewFoundation}
        onRegenerate={onRegenerateFoundation}
      />
    );
  if (event.event_type === "action_proposal_confirmed")
    return (
      <div className="binding-divider">
        <span>已确认操作，正在执行</span>
      </div>
    );
  if (event.event_type === "action_proposal_cancelled")
    return (
      <div className="binding-divider">
        <span>已取消待确认操作</span>
      </div>
    );
  if (event.event_type.startsWith("action_")) {
    const action = text(payload.action);
    const label = displayActionLabel(action, text(payload.label), actions);
    const isChatAction = ["chat", "session_action", "自由对话"].includes(action) || label === "自由对话";
    // 普通对话不显示冗余的流程横条；仅在 Main Agent 真正流出 reply
    // 字段时显示临时助手气泡，最终消息落库后会替换它。
    if (isChatAction) {
      let preview = "";
      for (const item of progress) {
        const itemPayload = obj(item.payload);
        if (["stream_reset", "preview_reset"].includes(item.event_type) && text(itemPayload.field) === "reply") preview = "";
        if (item.event_type === "preview_snapshot" && text(itemPayload.field) === "reply") preview = text(itemPayload.text);
        if (["content_delta", "preview_delta"].includes(item.event_type) && text(itemPayload.field) === "reply") preview += text(itemPayload.delta);
      }
      return preview ? (
        <article className="message assistant stream-reply-preview" aria-live="polite">
          <div className="message-content"><Text value={preview} /></div>
        </article>
      ) : event.event_type === "action_started" ? (
        // 首个文本分片到达前给出轻量反馈，避免普通聊天看起来像完全没有响应。
        <div className="chat-thinking" role="status">正在思考…</div>
      ) : null;
    }
    const detail =
      text(payload.error) ||
      text(payload.summary) ||
      (event.event_type === "action_started"
        ? "正在处理预设动作……"
        : event.event_type === "action_failed"
          ? "操作失败。"
          : "操作已完成。");
    return (
      <details
        className={
          event.event_type === "action_failed"
            ? "run-disclosure failed"
            : "run-disclosure"
        }
        open={event.event_type === "action_started"}
        onToggle={(event) => setProcessOpen(event.currentTarget.open)}
      >
        <summary>
          <span>{event.event_type === "action_started" ? `正在${label}` : event.event_type === "action_failed" ? `${label}失败` : `已完成 · ${label}`}</span>
          <em>
            {event.event_type === "action_started"
              ? ""
              : event.event_type === "action_failed"
                ? "失败"
                : "查看过程"}
          </em>
        </summary>
        <div className="event-detail">
          {progress.length > 0 ? <RunProgress events={progress} /> : processOpen && (
            <HistoricalRunProgress jobId={text(payload.run_id)} />
          )}
          {event.event_type !== "action_completed" && detail}
          {event.event_type === "action_completed" && progress.length === 0 && text(payload.summary)}
          {event.event_type === "action_failed" && text(payload.run_id) && (
            <button
              className="event-retry"
              onClick={() => onRetry(text(payload.run_id))}
            >
              重试
            </button>
          )}
        </div>
      </details>
    );
  }
  return null;
}

function displayActionLabel(action: string, label: string, actions: Record<string, string>) {
  const fallback: Record<string, string> = {
    prepare_chapter: "章节规划",
    write_next: "章节规划",
    revise_plan: "调整章节计划",
    revise_chapter_plan: "调整章节计划",
    cancel_plan: "取消候选计划",
    cancel_chapter_plan: "取消候选计划",
    confirm_plan: "生成本章",
    confirm_chapter_plan: "生成本章",
    approve_plan: "批准章节计划",
    approve_chapter_plan: "批准章节计划",
    write_from_plan: "按计划生成本章",
    rewrite_chapter: "重写章节",
    write_batch: "连续创作",
    start_chapter_batch: "连续创作",
    create_novel: "创建小说",
    confirm_foundation: "确认基础资料",
  };
  return actions[action] || fallback[action] || (label && !label.includes("_") ? label : "创作任务");
}

/** 章节正文以轻量 Markdown 阅读容器展示；不引入额外解析依赖。 */
function ChapterCompletionCard({ result }: { result: Json }) {
  const title = text(result.title) || "未命名章节";
  const chapterNumber = text(result.chapter_number);
  const content = text(result.content);
  const review = text(result.review_summary);
  const summary = text(result.chapter_summary);
  const committed = Boolean(result.committed);
  return (
    <article className="chapter-completion-card">
      <header>
        <div>
          <h3>第 {chapterNumber} 章{committed ? "已提交" : "候选正文"}</h3>
          <p>{title}</p>
        </div>
        <span className={committed ? "chapter-result-status" : "chapter-result-status pending"}>
          {committed ? "已提交" : "未提交"}
        </span>
      </header>
      <div className="chapter-result-metrics">
        <span><b>字数</b>{text(result.word_count)} 字</span>
        <span><b>修订</b>{text(result.revision_count)} 轮</span>
      </div>
      <details className="chapter-result-details">
        <summary>审查与章节状态</summary>
        <section><h4>审查</h4><p>{review || "未提供"}</p></section>
        <section>
          <h4>章节状态摘要</h4>
          <p>{summary || (committed ? "状态分析未提供章节摘要。" : "候选正文未提交，不生成章节状态摘要。")}</p>
        </section>
      </details>
      <details className="chapter-markdown-container">
        <summary>阅读全文</summary>
        <div className="markdown-content">
          {content.split(/\n{2,}/).filter(Boolean).map((paragraph, index) => (
            <p key={`${index}-${paragraph.slice(0, 16)}`}>{paragraph}</p>
          ))}
        </div>
      </details>
    </article>
  );
}
function ActionProposalCard({
  proposal,
  onConfirm,
  onCancel,
  onView,
  onRegenerate,
}: {
  proposal: ActionProposal;
  onConfirm: (id: string, actionType: string, version?: number) => void;
  onCancel: (id: string) => void;
  onView: (proposal: ActionProposal) => void;
  onRegenerate: (proposal: ActionProposal) => void;
}) {
  const id = text(proposal.action_proposal_id);
  const actionType = text(proposal.action_type);
  const payload = obj(proposal.payload);
  const candidate = obj(payload.candidate);
  const metadata = obj(candidate.metadata);
  const foundation = obj(candidate.foundation);
  const version = Number(payload.version || 1);
  const generationAttempt = Number(payload.generation_attempt || 1);
  if (actionType === "confirm_foundation") {
    return (
      <article className="chapter-plan-card pending action-proposal-card foundation-proposal-card">
        <header>
          <div><h3>故事基础资料</h3></div>
          <span className="plan-status">待确认</span>
        </header>
        <h4>《{text(metadata.title)}》</h4>
        <p>{text(metadata.genre)} · {text(metadata.target_chapters)} 章 · 每章约 {text(metadata.chapter_target_words)} 字</p>
        <section className="foundation-proposal-premise">
          <span>故事前提</span>
          <p>{text(foundation.premise)}</p>
        </section>
        <div className="foundation-proposal-metrics">
          <span>{list(foundation.characters).length} 名人物</span>
          <span>{list(foundation.initial_hooks).length} 条待埋伏笔</span>
          <span>{list(foundation.outline).length} 个总纲节点</span>
          <span>已生成 {generationAttempt}/3 次</span>
        </div>
        <div className="plan-actions foundation-proposal-actions">
          <button className="secondary-button" onClick={() => onView(proposal)}>查看并编辑</button>
          <button
            className="secondary-button"
            disabled={generationAttempt >= 3}
            title={generationAttempt >= 3 ? "已达到生成次数上限" : undefined}
            onClick={() => onRegenerate(proposal)}
          >
            {generationAttempt >= 3 ? "已达生成上限" : "重新生成"}
          </button>
          <button className="primary-button" onClick={() => onConfirm(id, actionType, version)}>确认基础资料</button>
        </div>
      </article>
    );
  }
  return (
    <article className="chapter-plan-card pending action-proposal-card">
      <header>
        <div>
          <h3>需要你的确认</h3>
        </div>
        <span className="plan-status">等待确认</span>
      </header>
      <p>{text(proposal.summary)}</p>
      {actionType === "create_novel" && (
        <div className="plan-grid">
          <section><span>题材</span><p>{text(payload.genre)}</p></section>
          <section><span>主角</span><p>{text(payload.protagonist)}</p></section>
          <section className="plan-wide"><span>故事前提</span><p>{text(payload.premise)}</p></section>
          <section className="plan-wide"><span>核心冲突</span><p>{text(payload.central_conflict)}</p></section>
          <section><span>基调</span><p>{text(payload.tone)}</p></section>
          <section><span>规模</span><p>{text(payload.target_chapters)} 章 · 每章约 {text(payload.chapter_target_words)} 字</p></section>
        </div>
      )}
      <div className="plan-actions">
        <button className="primary-button" onClick={() => onConfirm(id, actionType)}>
          确认并执行
        </button>
        <button className="plan-cancel" onClick={() => onCancel(id)}>
          取消
        </button>
      </div>
    </article>
  );
}

function FoundationEditor({
  proposal,
  onClose,
  onSaved,
  onConfirm,
  onError,
}: {
  proposal: ActionProposal;
  onClose: () => void;
  onSaved: (proposal: ActionProposal) => void;
  onConfirm: (proposalId: string, version: number) => void;
  onError: (message: string) => void;
}) {
  const payload = obj(proposal.payload);
  const actionType = text(proposal.action_type);
  const isRevision = actionType === "apply_foundation_revision";
  const revisionScope = text(payload.scope);
  const candidate = obj(payload.candidate);
  const metadata = obj(candidate.metadata);
  const initial = obj(candidate.foundation);
  const proposalId = text(proposal.action_proposal_id);
  const storageKey = `storyweaver:foundation-draft:${proposalId}`;
  const [draft, setDraft] = useState<Json>(initial);
  const [version, setVersion] = useState(Number(payload.version || 1));
  const [section, setSection] = useState(
    isRevision && revisionScope === "outline" ? "outline" : "premise",
  );
  const [selectedId, setSelectedId] = useState("");
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [exitOpen, setExitOpen] = useState(false);
  const [recovery, setRecovery] = useState<Json | null>(null);

  useEffect(() => {
    try {
      const saved = window.localStorage.getItem(storageKey);
      if (saved) setRecovery(obj(JSON.parse(saved)));
    } catch {
      window.localStorage.removeItem(storageKey);
    }
  }, [storageKey]);

  useEffect(() => {
    if (!dirty) return;
    try { window.localStorage.setItem(storageKey, JSON.stringify(draft)); } catch { /* 本地备份失败不影响编辑 */ }
  }, [dirty, draft, storageKey]);

  useEffect(() => {
    if (!dirty) return;
    const warn = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = ""; };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [dirty]);

  const update = (next: Json) => { setDraft(next); setDirty(true); };
  const setField = (field: string, value: unknown) => update({ ...draft, [field]: value });
  const updateItem = (collection: string, idField: string, id: string, changes: Json) => {
    update({
      ...draft,
      [collection]: list(draft[collection]).map((value) => {
        const item = obj(value);
        return text(item[idField]) === id ? { ...item, ...changes } : item;
      }),
    });
  };
  const lineList = (value: unknown) => list(value).map(text).join("\n");
  const parseLines = (value: string) => value.split("\n").map((item) => item.trim()).filter(Boolean);
  const buildPatch = (): Json => {
    const full: Json = {
    premise: text(draft.premise).trim(),
    world_setting: text(draft.world_setting).trim(),
    central_conflict: text(draft.central_conflict).trim(),
    ending_direction: text(draft.ending_direction).trim(),
    writing_rules: list(draft.writing_rules).map(text).filter(Boolean),
    characters: list(draft.characters).map((value) => {
      const item = obj(value);
      return {
        character_id: text(item.character_id), name: text(item.name), role: text(item.role),
        personality: list(item.personality).map(text).filter(Boolean), motivation: text(item.motivation),
        long_term_goal: text(item.long_term_goal), conflict: text(item.conflict),
        speech_style: text(item.speech_style), knowledge_boundaries: list(item.knowledge_boundaries).map(text).filter(Boolean),
      };
    }),
    outline: list(draft.outline).map((value) => {
      const item = obj(value);
      return {
        node_id: text(item.node_id), title: text(item.title), chapter_start: Number(item.chapter_start),
        chapter_end: Number(item.chapter_end), goal: text(item.goal),
        expected_changes: list(item.expected_changes).map(text).filter(Boolean),
      };
    }),
    initial_hooks: list(draft.initial_hooks).map((value) => {
      const item = obj(value);
      return {
        hook_id: text(item.hook_id), name: text(item.name), description: text(item.description),
        importance: Number(item.importance), expected_payoff: text(item.expected_payoff),
      };
    }),
    };
    if (!isRevision) return full;
    if (revisionScope === "outline") return { outline: full.outline };
    return {
      premise: full.premise,
      world_setting: full.world_setting,
      central_conflict: full.central_conflict,
      ending_direction: full.ending_direction,
      writing_rules: full.writing_rules,
      characters: full.characters,
    };
  };
  const save = useCallback(async () => {
    if (saving) return false;
    setSaving(true);
    try {
      const result = await api.updateFoundationProposal(proposalId, version, buildPatch());
      const nextVersion = Number(obj(result.proposal.payload).version || version + 1);
      setVersion(nextVersion);
      setDirty(false);
      window.localStorage.removeItem(storageKey);
      onSaved(result.proposal);
      return true;
    } catch (error) {
      onError(errorMessage(error));
      return false;
    } finally {
      setSaving(false);
    }
  }, [draft, onError, onSaved, proposalId, saving, storageKey, version]);

  useEffect(() => {
    if (!dirty || saving) return;
    const timer = window.setTimeout(() => { void save(); }, 1200);
    return () => window.clearTimeout(timer);
  }, [dirty, save, saving]);

  const requestClose = () => {
    if (dirty || saving) setExitOpen(true);
    else onClose();
  };
  const characters = list(draft.characters).map(obj);
  const outline = list(draft.outline).map(obj);
  const hooks = list(draft.initial_hooks).map(obj);
  const selectedCharacter = characters.find((item) => text(item.character_id) === selectedId) || characters[0];
  const selectedOutline = outline.find((item) => text(item.node_id) === selectedId) || outline[0];
  const selectedHook = hooks.find((item) => text(item.hook_id) === selectedId) || hooks[0];
  const selectItem = (nextSection: string, id: string) => { setSection(nextSection); setSelectedId(id); };
  const allSections = [
    ["premise", "故事前提"], ["world", "世界设定"], ["conflict", "核心冲突"],
    ["characters", "人物"], ["outline", "故事总纲"], ["hooks", "初始伏笔"], ["rules", "写作规则"],
  ];
  const sections = isRevision
    ? allSections.filter(([id]) => revisionScope === "outline" ? id === "outline" : id !== "outline" && id !== "hooks")
    : allSections;

  return (
    <section className="foundation-editor" role="dialog" aria-modal="true" aria-label="编辑故事基础资料">
      <header className="foundation-editor-header">
        <div><span>{isRevision ? (revisionScope === "outline" ? "后续大纲修订 · 待确认" : "故事设定修订 · 待确认") : "故事基础资料 · 待确认"}</span><h1>《{text(metadata.title)}》</h1><p>{text(metadata.genre)} · {text(metadata.target_chapters)} 章 · 每章约 {text(metadata.chapter_target_words)} 字</p></div>
        <button className="secondary-button" onClick={requestClose}>返回</button>
      </header>
      {recovery && <div className="foundation-recovery"><span>发现未同步的本地草稿。</span><button onClick={() => { setDraft(recovery); setDirty(true); setRecovery(null); }}>恢复草稿</button><button onClick={() => { window.localStorage.removeItem(storageKey); setRecovery(null); }}>忽略</button></div>}
      <div className="foundation-editor-body">
        <nav className="foundation-editor-nav" aria-label="基础资料目录">
          {sections.map(([id, label]) => <button key={id} className={section === id ? "active" : ""} onClick={() => setSection(id)}>{label}</button>)}
        </nav>
        <main className="foundation-editor-content">
          {section === "premise" && <EditorText label="故事前提" value={text(draft.premise)} onChange={(value) => setField("premise", value)} />}
          {section === "world" && <EditorText label="世界设定" value={text(draft.world_setting)} onChange={(value) => setField("world_setting", value)} />}
          {section === "conflict" && <><EditorText label="核心冲突" value={text(draft.central_conflict)} onChange={(value) => setField("central_conflict", value)} /><EditorText label="结局方向" value={text(draft.ending_direction)} onChange={(value) => setField("ending_direction", value)} /></>}
          {section === "rules" && <EditorText label="写作规则" hint="每行一条规则" value={lineList(draft.writing_rules)} onChange={(value) => setField("writing_rules", parseLines(value))} />}
          {section === "characters" && <div className="foundation-module"><h2>人物</h2><div className="foundation-card-list">{characters.map((item) => <button key={text(item.character_id)} className={text(selectedCharacter?.character_id) === text(item.character_id) ? "selected" : ""} onClick={() => selectItem("characters", text(item.character_id))}><strong>{text(item.name)}</strong><span>{text(item.role)}</span><small>{list(item.personality).map(text).join(" · ")}</small></button>)}</div>{selectedCharacter && <div className="foundation-detail"><h3>{text(selectedCharacter.name)}</h3><EditorInput label="角色定位" value={text(selectedCharacter.role)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { role: value })} /><EditorInput label="人物动机" value={text(selectedCharacter.motivation)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { motivation: value })} multiline /><EditorInput label="长期目标" value={text(selectedCharacter.long_term_goal)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { long_term_goal: value })} multiline /><EditorInput label="人物冲突" value={text(selectedCharacter.conflict)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { conflict: value })} multiline /><EditorInput label="性格" hint="每行一个特征" value={lineList(selectedCharacter.personality)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { personality: parseLines(value) })} multiline /><EditorInput label="说话风格" value={text(selectedCharacter.speech_style)} onChange={(value) => updateItem("characters", "character_id", text(selectedCharacter.character_id), { speech_style: value })} multiline /></div>}</div>}
          {section === "outline" && <div className="foundation-module"><h2>故事总纲 <small>{outline.length} 个阶段</small></h2><div className="foundation-outline-editor"><div className="foundation-outline-list">{outline.map((item) => <button key={text(item.node_id)} className={text(selectedOutline?.node_id) === text(item.node_id) ? "selected" : ""} onClick={() => selectItem("outline", text(item.node_id))}><span>第 {text(item.chapter_start)}–{text(item.chapter_end)} 章</span><strong>{text(item.title)}</strong><small>{text(item.goal)}</small></button>)}</div>{selectedOutline && <div className="foundation-detail foundation-outline-detail"><h3>{text(selectedOutline.title)}</h3><EditorInput label="阶段标题" value={text(selectedOutline.title)} onChange={(value) => updateItem("outline", "node_id", text(selectedOutline.node_id), { title: value })} /><div className="foundation-number-grid"><EditorInput label="起始章节" value={text(selectedOutline.chapter_start)} onChange={(value) => updateItem("outline", "node_id", text(selectedOutline.node_id), { chapter_start: Number(value) })} type="number" /><EditorInput label="结束章节" value={text(selectedOutline.chapter_end)} onChange={(value) => updateItem("outline", "node_id", text(selectedOutline.node_id), { chapter_end: Number(value) })} type="number" /></div><EditorInput label="阶段目标" value={text(selectedOutline.goal)} onChange={(value) => updateItem("outline", "node_id", text(selectedOutline.node_id), { goal: value })} multiline /><EditorInput label="预期变化" hint="每行一条" value={lineList(selectedOutline.expected_changes)} onChange={(value) => updateItem("outline", "node_id", text(selectedOutline.node_id), { expected_changes: parseLines(value) })} multiline /></div>}</div></div>}
          {section === "hooks" && <div className="foundation-module"><h2>初始伏笔</h2><div className="foundation-card-list">{hooks.map((item) => <button key={text(item.hook_id)} className={text(selectedHook?.hook_id) === text(item.hook_id) ? "selected" : ""} onClick={() => selectItem("hooks", text(item.hook_id))}><strong>{text(item.name) || text(item.description)}</strong><span>重要度 {text(item.importance)}</span><small>{text(item.expected_payoff)}</small></button>)}</div>{selectedHook && <div className="foundation-detail"><h3>{text(selectedHook.name) || "伏笔"}</h3><EditorInput label="伏笔名称" value={text(selectedHook.name)} onChange={(value) => updateItem("initial_hooks", "hook_id", text(selectedHook.hook_id), { name: value })} /><EditorInput label="伏笔内容" value={text(selectedHook.description)} onChange={(value) => updateItem("initial_hooks", "hook_id", text(selectedHook.hook_id), { description: value })} multiline /><EditorInput label="预期回收" value={text(selectedHook.expected_payoff)} onChange={(value) => updateItem("initial_hooks", "hook_id", text(selectedHook.hook_id), { expected_payoff: value })} multiline /><EditorInput label="重要度（1–5）" value={text(selectedHook.importance)} onChange={(value) => updateItem("initial_hooks", "hook_id", text(selectedHook.hook_id), { importance: Number(value) })} type="number" /></div>}</div>}
        </main>
      </div>
      <footer className="foundation-editor-footer"><span>{saving ? "正在保存…" : dirty ? "尚有未保存修改" : "已保存到候选方案"}</span><div><button className="secondary-button" disabled={saving || !dirty} onClick={() => void save()}>保存修改</button><button className="primary-button" disabled={dirty || saving || Boolean(recovery)} onClick={() => onConfirm(proposalId, version)}>{isRevision ? "确认修订" : "确认基础资料"}</button></div></footer>
      {exitOpen && <div className="foundation-exit-mask"><section><h2>尚有未保存修改</h2><p>保存后仍是待确认方案，不会提交为正式作品。</p><div><button className="secondary-button" onClick={() => setExitOpen(false)}>继续编辑</button><button className="secondary-button" onClick={() => { window.localStorage.removeItem(storageKey); onClose(); }}>放弃修改</button><button className="primary-button" disabled={saving} onClick={() => void save().then((ok) => { if (ok) onClose(); })}>保存并退出</button></div></section></div>}
    </section>
  );
}

function EditorText({ label, hint, value, onChange }: { label: string; hint?: string; value: string; onChange: (value: string) => void }) {
  return <label className="foundation-field"><strong>{label}</strong>{hint && <small>{hint}</small>}<textarea value={value} onChange={(event) => onChange(event.target.value)} /></label>;
}

function EditorInput({ label, hint, value, onChange, multiline = false, type = "text" }: { label: string; hint?: string; value: string; onChange: (value: string) => void; multiline?: boolean; type?: string }) {
  return <label className="foundation-field"><strong>{label}</strong>{hint && <small>{hint}</small>}{multiline ? <textarea value={value} onChange={(event) => onChange(event.target.value)} /> : <input type={type} value={value} min={type === "number" ? 1 : undefined} max={type === "number" ? 5 : undefined} onChange={(event) => onChange(event.target.value)} />}</label>;
}
function Text({ value }: { value: string }) {
  return (
    <>
      {value.split("\n").map((line, index) => (
        <span key={index}>
          {line}
          {index < value.split("\n").length - 1 && <br />}
        </span>
      ))}
    </>
  );
}
/** 展开历史任务时回放持久事件，不触发任务重试或切换当前会话。 */
function HistoricalRunProgress({ jobId }: { jobId: string }) {
  const [events, setEvents] = useState<any[]>([]);
  const [status, setStatus] = useState<"loading" | "streaming" | "complete" | "error">("loading");
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    setEvents([]);
    setStatus("loading");
    if (!jobId) {
      setStatus("complete");
      return;
    }
    let active = true;
    const seen = new Set<number>();
    const source = api.streamJob(jobId, (event) => {
      if (!active) return;
      // 持久事件按序号去重；临时预览没有持久序号，仍按流处理。
      if (event.sequence > 0) {
        if (seen.has(event.sequence)) return;
        seen.add(event.sequence);
      }
      setEvents((old) => [...old, event]);
      const terminal = ["job_succeeded", "job_failed", "job_paused", "job_interrupted", "job_cancelled"].includes(event.event_type);
      setStatus(terminal ? "complete" : "streaming");
      if (terminal) source.close();
    });
    source.onerror = () => {
      if (!active) return;
      source.close();
      setStatus("error");
    };
    return () => {
      active = false;
      source.close();
    };
  }, [jobId, attempt]);

  const hasStages = events.some((event) => event.event_type === "stage_started");
  return <>
    {hasStages && <RunProgress events={events} />}
    {status === "loading" && <p role="status">正在加载执行过程…</p>}
    {status === "streaming" && !hasStages && <p role="status">等待执行步骤…</p>}
    {status === "complete" && !hasStages && <p>该操作没有详细执行步骤。</p>}
    {status === "error" && <p role="alert">执行过程加载失败。<button className="event-retry" onClick={() => setAttempt((value) => value + 1)}>重新加载</button></p>}
  </>;
}

function RunProgress({ events }: { events: any[] }) {
  if (!events?.length) return null;
  // 结构化 Worker 只会对显式声明的可预览字段发送临时内存增量；这里不解析
  // JSON，也不把内部协议暴露给用户。最终提交后完成卡会替换这段预览。
  let preview = "";
  for (const event of events) {
    const payload = obj(event.payload);
    if (["stream_reset", "preview_reset"].includes(event.event_type) && text(payload.field) === "content") preview = "";
    if (event.event_type === "preview_snapshot" && text(payload.field) === "content") preview = text(payload.text);
    if (["content_delta", "preview_delta"].includes(event.event_type) && text(payload.field) === "content") preview += text(payload.delta);
  }
  const stages: Array<{
    agentId: string;
    name: string;
    status: "running" | "completed" | "failed";
    elapsed?: number;
    tokens?: number;
    error?: string;
    models: any[];
    tools: any[];
  }> = [];
  for (const event of events) {
    const payload = obj(event.payload);
    if (event.event_type === "stage_started") {
      stages.push({
        agentId: text(payload.agent_id),
        name: text(payload.display_name) || text(payload.agent_id) || "Agent",
        status: "running",
        models: [],
        tools: [],
      });
      continue;
    }
    const stage = [...stages]
      .reverse()
      .find(
        (item) =>
          item.agentId === text(payload.agent_id) && item.status === "running",
      );
    if (!stage) continue;
    if (event.event_type === "model_completed") stage.models.push(payload);
    if (event.event_type === "tool_completed") stage.tools.push(payload);
    if (
      event.event_type === "stage_completed" ||
      event.event_type === "stage_failed"
    ) {
      stage.status =
        event.event_type === "stage_failed" ? "failed" : "completed";
      stage.elapsed = Number(payload.elapsed_seconds || 0);
      stage.tokens = Number(payload.total_tokens || 0);
      stage.error = text(payload.error);
    }
  }
  return (
    <>
    <ol className="run-progress-list">
      {stages.map((stage, index) => (
        <li
          key={`${stage.agentId}-${index}`}
          className={`run-stage ${stage.status}`}
        >
          <span className="run-stage-icon">
            {stage.status === "completed" ? (
              "✓"
            ) : stage.status === "failed" ? (
              "×"
            ) : (
              <i className="progress-spinner" />
            )}
          </span>
          <span className="run-stage-name">{stage.name}</span>
          <span className="run-stage-metrics">
            {stage.elapsed ? `${stage.elapsed.toFixed(1)}s` : ""}
            {stage.elapsed && stage.tokens ? " · " : ""}
            {stage.tokens
              ? `${stage.tokens.toLocaleString()} Token`
              : stage.status === "running"
                ? "执行中"
                : ""}
          </span>
          {stage.error && <small>{stage.error}</small>}
          {stage.models.length > 0 && (
            <section className="run-detail-group" aria-label="模型回合">
              <div className="run-detail-label">模型回合</div>
              <ul className="run-model-list">
                {stage.models.map((model, modelIndex) => {
                  const detail = formatModelEvent(model);
                  return <li key={modelIndex} className="run-detail-row">
                    <span className="run-detail-description">{detail.label}</span>
                    <span className="run-detail-metrics">{detail.metrics}</span>
                  </li>;
                })}
              </ul>
            </section>
          )}
          {stage.tools.length > 0 && (
            <section className="run-detail-group" aria-label="资料查询">
              <div className="run-detail-label">资料查询</div>
              <ul className="run-tool-list">
                {stage.tools.map((tool, toolIndex) => {
                  const detail = formatToolEvent(tool);
                  const state = tool.budget_exhausted === true ? "exhausted" : tool.succeeded === false ? "failed" : "";
                  return <li key={toolIndex} className={`run-detail-row ${state}`}>
                    <span className="run-detail-description">
                      <span className={`run-tool-status ${state}`}>{detail.status}</span>
                      <span>{detail.action}</span>
                    </span>
                    <span className="run-detail-metrics">{detail.metrics}</span>
                    {detail.error && <span className="run-detail-error">{detail.error}</span>}
                  </li>;
                })}
              </ul>
            </section>
          )}
        </li>
      ))}
    </ol>
    {preview && (
      <section className="stream-preview" aria-live="polite">
        <div className="stream-preview-label">正文生成中</div>
        <Text value={preview} />
      </section>
    )}
    </>
  );
}

function formatModelEvent(payload: Json): { label: string; metrics: string } {
  const step = Number(payload.step || 0),
    maximum = Number(payload.max_steps || 0);
  const round = step && maximum ? `第 ${step}/${maximum} 回合` : "模型回合";
  const kind = text(payload.response_kind);
  const result =
    kind === "research"
      ? "检索证据"
      : kind === "tool_calls"
        ? `请求工具 ${Number(payload.tool_call_count || 0)} 个`
        : kind === "final"
          ? "输出最终结果"
          : "未返回有效结果";
  return {
    label: `${round} · ${result}`,
    metrics: `输入 ${Number(payload.input_tokens || 0).toLocaleString()} / 输出 ${Number(payload.output_tokens || 0).toLocaleString()} Token${Number(payload.elapsed_seconds || 0) ? ` · ${Number(payload.elapsed_seconds).toFixed(2)}s` : ""}`,
  };
}

function formatToolEvent(payload: Json): { status: string; action: string; metrics: string; error: string } {
  const exhausted = payload.budget_exhausted === true;
  const status =
    exhausted
      ? "未执行"
      : payload.deduplicated === true
        ? "复用结果"
        : payload.succeeded === false
          ? "失败"
          : "成功";
  const index = Number(payload.tool_call_index || 0),
    limit = Number(payload.tool_call_limit || 0);
  const action = describeToolAction(text(payload.tool_name), obj(payload.tool_arguments));
  return {
    status, action,
    metrics: exhausted ? "" : [index && limit ? `额度 ${index}/${limit}` : "", Number(payload.elapsed_seconds || 0) ? `${Number(payload.elapsed_seconds).toFixed(2)}s` : ""].filter(Boolean).join(" · "),
    error: exhausted ? `原因：查询额度已用完${index && limit ? `（${index}/${limit}）` : ""}` : text(payload.error),
  };
}

/** 将内部工具调用投影为用户可读的动作，不暴露函数名称或实现细节。 */
function describeToolAction(toolName: string, argumentsValue: Json): string {
  const query = text(argumentsValue.query).trim();
  const limit = Number(argumentsValue.limit || 0);
  const compactQuery = query.length > 36 ? `${query.slice(0, 36)}…` : query;
  if (toolName === "read_chapter_summary") {
    const chapter = Number(argumentsValue.chapter_number || 0);
    return chapter ? `读取第 ${chapter} 章摘要` : "读取章节摘要";
  }
  if (toolName === "list_open_foreshadowings") {
    return limit ? `查看未解伏笔（最多 ${limit} 条）` : "查看未解伏笔";
  }
  if (toolName === "get_entity_evidence") {
    return "核对人物设定";
  }
  if (toolName === "search_canon_evidence") {
    return compactQuery ? `检索正史证据：「${compactQuery}」` : "检索正史证据";
  }
  if (toolName === "query_foundation") {
    const section = text(argumentsValue.section);
    const scope = section === "characters" ? "人物设定" : section === "world" ? "世界规则" : section === "writing_rules" ? "写作规则" : "基础设定";
    return compactQuery ? `查询${scope}：「${compactQuery}」` : `查询${scope}`;
  }
  return "读取创作资料";
}
function PlanCard({ plan, onConfirm, onCancel, onRevise }: any) {
  const pending = plan.status === "pending";
  const approved = plan.status === "approved";
  const confirmed = plan.status === "confirmed";
  const appliedSkills = list(plan.applied_skills);
  const row = (label: string, value: any) => (
    <section>
      <span>{label}</span>
      {Array.isArray(value) ? (
        <ul>
          {value.map((item) => (
            <li key={String(item)}>
              {text(item.name || item.description || item)}
            </li>
          ))}
        </ul>
      ) : (
        <p>{text(value) || "未提供"}</p>
      )}
    </section>
  );
  return (
    <article
      className={pending ? "chapter-plan-card pending" : "chapter-plan-card"}
    >
      <header>
        <div>
          <h3>
            第 {plan.chapter_number} 章{pending ? "候选计划" : approved ? "已批准计划" : confirmed ? "已确认计划" : "已执行计划"}
          </h3>
          {appliedSkills.length > 0 && (
            <div className="plan-skill-row">
              {appliedSkills.map((item) => (
                <span
                  key={`${text(obj(item).skill_id)}-${text(obj(item).content_hash)}`}
                >
                  已应用 Skill：{text(obj(item).skill_id)} ·{" "}
                  {text(obj(item).content_hash).slice(0, 8)}
                </span>
              ))}
            </div>
          )}
        </div>
        <span className="plan-status">
          {pending
            ? "等待确认"
            : approved
              ? "已批准，尚未写作"
            : confirmed
              ? "已确认"
              : text(plan.status)}
        </span>
      </header>
      <div className="plan-grid">
        <section className="plan-wide">
          <span>本章目标</span>
          <p>{text(plan.goal) || "未提供"}</p>
        </section>
        {row("地点", plan.location)}
        {row("当前时间", plan.current_time)}
        {row(
          "出场人物",
          list(plan.participants).map(
            (item) => obj(item).name || obj(item).character_id,
          ),
        )}
        {row("必须发生", plan.required_beats)}
        {row("禁止发生", plan.forbidden_events)}
        {row("推进伏笔", plan.relevant_hooks)}
        {row("风格重点", plan.style_focus)}
        <section className="plan-wide">
          <span>结尾悬念</span>
          <p>{text(plan.ending_hook) || "未提供"}</p>
        </section>
      </div>
      <footer>
        <span>目标约 {plan.target_words || 0} 字</span>
      </footer>
      {(pending || approved) && (
        <div className="plan-actions">
          <button
            className="primary-button"
            onClick={() => onConfirm(plan.proposal_id, plan.status)}
          >
            {approved ? "按此计划开始写作" : "批准计划"}
          </button>
          {pending && <button
            className="secondary-button"
            onClick={() => onRevise(plan.proposal_id)}
          >
            调整计划
          </button>}
          <button
            className="plan-cancel"
            onClick={() => onCancel(plan.proposal_id)}
          >
            取消
          </button>
        </div>
      )}
    </article>
  );
}
function WorkView({
  project,
  chapter,
  bound,
  onWrite,
  onBatch,
  onChapter,
  onCloseChapter,
  onRewrite,
  onReviseFoundation,
}: {
  project: Json | null;
  chapter: Json | null;
  bound: boolean;
  onWrite: () => void;
  onBatch: () => void;
  onChapter: (number: number) => Promise<void>;
  onCloseChapter: () => void;
  onRewrite: () => void;
  onReviseFoundation: (scope: "outline" | "setting") => void;
}) {
  const [section, setSection] = useState<"overview" | "outline" | "setting">("overview");
  const [expandedOutlineIds, setExpandedOutlineIds] = useState<string[] | null>(null);
  const [settingSection, setSettingSection] = useState<"basics" | "characters" | "world" | "direction">("basics");
  const [selectedCharacterId, setSelectedCharacterId] = useState<string | null>(null);
  const [showAllHooks, setShowAllHooks] = useState(false);
  const [overviewPanels, setOverviewPanels] = useState({ chapters: true, characters: true, hooks: true });
  if (chapter)
    return (
      <div className="work-container">
        <ChapterReader
          chapter={chapter}
          onClose={onCloseChapter}
          onRewrite={onRewrite}
        />
      </div>
    );
  if (!project)
    return (
      <div className="work-container">
        <div className="work-header">
          <div>
            <h1>选择一部作品</h1>
            <p>从左侧“我的作品”选择作品，或创建一部新小说。</p>
          </div>
        </div>
        <div className="work-empty">当前会话没有关联作品。</div>
      </div>
    );
  const hooks = list(project.hooks),
    chapters = list(project.chapters),
    characters = list(project.characters),
    foundation = obj(project.foundation),
    outline = list(foundation.outline).map(obj),
    foundationCharacters = list(foundation.characters).map(obj),
    completedChapter = Number(project.last_committed_chapter || 0);
  const activeHooks = hooks.filter((item) => ["open", "progressing"].includes(text(obj(item).status)));
  const deferredHooks = hooks.filter((item) => text(obj(item).status) === "deferred");
  // 初始伏笔尚未写入正文时是 deferred，仍应在概览中作为“待埋”让作者看见。
  const overviewHooks = activeHooks.length ? activeHooks : deferredHooks;
  const visibleHooks = showAllHooks ? overviewHooks : overviewHooks.slice(0, 3);
  const selectedCharacter = foundationCharacters.find((item) => text(item.character_id) === selectedCharacterId) || foundationCharacters[0];
  const outlineStatus = (node: Json) => {
    const start = Number(node.chapter_start || 0), end = Number(node.chapter_end || 0);
    return end <= completedChapter ? "已进入 Canon" : start <= completedChapter + 1 ? "当前阶段" : "未来计划";
  };
  const expandOutline = (nodeId: string) => {
    setExpandedOutlineIds((previous) => {
      const current = previous ?? outline.filter((node) => outlineStatus(node) === "当前阶段").map((node) => text(node.node_id));
      return current.includes(nodeId) ? current.filter((id) => id !== nodeId) : [...current, nodeId];
    });
  };
  return (
    <div className="work-container">
      <div className="work-header">
        <div>
          <h1>{text(project.title)}</h1>
          <p>
            {text(project.genre)} · 每章约 {text(project.chapter_target_words)}{" "}
            字
          </p>
          <p className="work-session-note">
            {bound ? "当前聊天已绑定此作品。" : "当前仅查看此作品。"}
          </p>
        </div>
        <div className="work-header-actions">
          <button className="secondary-button" onClick={onBatch}>
            连续创作
          </button>
          <button className="primary-button" onClick={onWrite}>
            继续写作
          </button>
        </div>
      </div>
      <nav className="work-section-tabs" aria-label="作品内容">
        <button className={section === "overview" ? "active" : ""} onClick={() => setSection("overview")}>概览</button>
        <button className={section === "outline" ? "active" : ""} onClick={() => setSection("outline")}>故事大纲</button>
        <button className={section === "setting" ? "active" : ""} onClick={() => setSection("setting")}>故事设定</button>
      </nav>
      {section === "outline" ? (
        <div className="work-content outline-workspace">
          <div className="outline-toolbar">
            <p>已完成章节以 Canon 为准；后续节点是可调整的创作计划。</p>
            <div><button className="text-button" onClick={() => setExpandedOutlineIds(outline.map((node) => text(node.node_id)))}>展开全部</button><button className="secondary-button" onClick={() => onReviseFoundation("outline")}>调整后续大纲</button></div>
          </div>
          <article className="work-card outline-list-card">
            <div className="card-heading"><h2>当前规划</h2><span>第 1–{text(project.target_chapters)} 章 · {outline.length} 个节点</span></div>
            <div className="outline-node-list">
              {outline.map((node) => {
                const nodeId = text(node.node_id), status = outlineStatus(node);
                const expanded = expandedOutlineIds === null ? status === "当前阶段" : expandedOutlineIds.includes(nodeId);
                return (
                  <section className={expanded ? "expanded" : ""} key={nodeId}>
                    <button className="outline-node-summary" onClick={() => expandOutline(nodeId)} aria-expanded={expanded}>
                      <span className="outline-node-number">{String(node.chapter_start || "").padStart(2, "0")}</span>
                      <span><strong>{text(node.title)}</strong><small>{text(node.goal)}</small></span>
                      <em>第 {text(node.chapter_start)}–{text(node.chapter_end)} 章 · {status}</em>
                      <i aria-hidden="true">⌄</i>
                    </button>
                    {expanded && <div className="outline-node-detail">
                      {list(node.expected_changes).length > 0 && <><span>关键事件</span><ul>{list(node.expected_changes).map((item, index) => <li key={`${nodeId}-${index}`}>{text(item)}</li>)}</ul></>}
                    </div>}
                  </section>
              )})}
            </div>
          </article>
        </div>
      ) : section === "setting" ? (
        <div className="work-content setting-workspace">
          <div className="setting-toolbar"><p>这些资料作为后续创作依据；已完成章节以 Canon 为准。</p><button className="secondary-button" onClick={() => onReviseFoundation("setting")}>修订故事设定</button></div>
          <div className="setting-layout">
            <nav className="setting-nav" aria-label="故事设定目录">
              <button className={settingSection === "basics" ? "active" : ""} onClick={() => setSettingSection("basics")}>故事基础</button>
              <button className={settingSection === "characters" ? "active" : ""} onClick={() => setSettingSection("characters")}>人物 · {foundationCharacters.length}</button>
              <button className={settingSection === "world" ? "active" : ""} onClick={() => setSettingSection("world")}>世界与地点</button>
              <button className={settingSection === "direction" ? "active" : ""} onClick={() => setSettingSection("direction")}>核心冲突与结局方向</button>
            </nav>
            <article className="setting-detail">
              {settingSection === "basics" && <><span className="setting-kicker">故事基础</span><h2>故事前提</h2><p className="setting-prose">{text(foundation.premise)}</p><section className="setting-rules"><h3>写作规则</h3><ul className="writing-rule-list">{list(foundation.writing_rules).map((item, index) => <li key={index}>{text(item)}</li>)}</ul></section></>}
              {settingSection === "world" && <><span className="setting-kicker">世界与地点</span><h2>世界设定</h2><p className="setting-prose">{text(foundation.world_setting)}</p></>}
              {settingSection === "direction" && <><span className="setting-kicker">创作方向</span><h2>核心冲突</h2><p className="setting-prose">{text(foundation.central_conflict)}</p><hr /><h2>结局方向</h2><p className="setting-prose">{text(foundation.ending_direction)}</p></>}
              {settingSection === "characters" && <><span className="setting-kicker">人物 · {foundationCharacters.length}</span><h2>人物设定</h2><div className="setting-character-layout"><div className="setting-character-list">{foundationCharacters.map((item) => <button className={text(item.character_id) === text(selectedCharacter?.character_id) ? "selected" : ""} key={text(item.character_id)} onClick={() => setSelectedCharacterId(text(item.character_id))}><strong>{text(item.name)}</strong><span>{text(item.role)}</span><small>{text(item.long_term_goal)}</small></button>)}</div>{selectedCharacter && <section className="setting-character-detail"><h3>{text(selectedCharacter.name)}</h3><p>{text(selectedCharacter.role)}</p><dl><div><dt>长期目标</dt><dd>{text(selectedCharacter.long_term_goal)}</dd></div><div><dt>性格</dt><dd>{list(selectedCharacter.personality).map(text).join(" · ")}</dd></div><div><dt>说话风格</dt><dd>{text(selectedCharacter.speech_style)}</dd></div></dl></section>}</div></>}
            </article>
          </div>
        </div>
      ) : (
      <div className="work-content">
        <div className="work-progress-summary"><strong>已写 {completedChapter} / 计划 {text(project.target_chapters)} 章</strong></div>
        <section className="work-card overview-chapters-card overview-panel">
            <button className="overview-panel-toggle" onClick={() => setOverviewPanels((panels) => ({ ...panels, chapters: !panels.chapters }))} aria-expanded={overviewPanels.chapters}>
              <strong>章节</strong><span>{chapters.length} 章</span><i className="overview-panel-chevron" aria-hidden="true" />
            </button>
            {overviewPanels.chapters && <div className="overview-panel-body">
            <div className="detail-list">
              {chapters.length ? (
                chapters.map((item) => (
                  <button
                    key={text(obj(item).chapter_number)}
                    className="detail-item chapter-list-button"
                    onClick={() =>
                      void onChapter(Number(obj(item).chapter_number))
                    }
                  >
                    <strong>
                      第 {text(obj(item).chapter_number)} 章 ·{" "}
                      {text(obj(item).title)}
                    </strong>
                    <span>{text(obj(item).word_count)} 字</span>
                    <span className="status-pill">
                      {CHAPTER_STATUS[text(obj(item).status)] ||
                        text(obj(item).status)}
                    </span>
                  </button>
                ))
              ) : (
                <div className="chapter-empty"><strong>尚未生成章节</strong></div>
              )}
            </div>
            </div>}
        </section>
        <div className="work-columns overview-secondary-columns">
          <section className="work-card overview-panel">
            <button className="overview-panel-toggle" onClick={() => setOverviewPanels((panels) => ({ ...panels, characters: !panels.characters }))} aria-expanded={overviewPanels.characters}>
              <strong>人物状态</strong><span>{completedChapter ? "最新快照" : "初始资料"}</span><i className="overview-panel-chevron" aria-hidden="true" />
            </button>
            {overviewPanels.characters && <div className="overview-panel-body">
            <div className="character-state-list">
              {characters.map((item) => {
                const character = obj(item), characterId = text(character.character_id);
                const foundationCharacter = foundationCharacters.find((candidate) => text(candidate.character_id) === characterId);
                return <section key={characterId}>
                  <strong>{text(character.name)}</strong>
                  {completedChapter ? <><span>{text(character.status)} · {text(character.emotion)}</span><p>{text(character.location)} · 目标：{text(character.goal)}</p></> : <><span>{text(foundationCharacter?.role)}</span><p>目标：{text(character.goal)}</p></>}
                </section>;
              })}
              {!characters.length && <div className="empty-list">暂无人物状态</div>}
            </div>
            </div>}
          </section>
          <section className="work-card overview-panel">
            <button className="overview-panel-toggle" onClick={() => setOverviewPanels((panels) => ({ ...panels, hooks: !panels.hooks }))} aria-expanded={overviewPanels.hooks}>
              <strong>伏笔</strong><span>{activeHooks.length ? `${activeHooks.length} 条未解` : `${deferredHooks.length} 条待埋`}</span><i className="overview-panel-chevron" aria-hidden="true" />
            </button>
            {overviewPanels.hooks && <div className="overview-panel-body">
            <div className="detail-list">
              {visibleHooks.map((item) => (
                <div className="detail-item" key={text(obj(item).hook_id)}>
                  <strong>
                    {text(
                      obj(item).display_name ||
                        obj(item).name ||
                        obj(item).description,
                    )}
                  </strong>
                  <span>{text(obj(item).description)}</span>
                  <span className="status-pill">
                    {text(obj(item).status) === "deferred" ? "待埋" : HOOK_STATUS[text(obj(item).status)] ||
                      text(obj(item).status)}
                  </span>
                </div>
              ))}
              {overviewHooks.length > visibleHooks.length && <button className="text-button hook-more" onClick={() => setShowAllHooks(true)}>查看全部伏笔</button>}
              {!visibleHooks.length && <div className="empty-list">暂无伏笔</div>}
            </div>
            </div>}
          </section>
        </div>
      </div>
      )}
    </div>
  );
}
function ChapterReader({ chapter, onClose, onRewrite }: any) {
  return (
    <article className="chapter-reader">
      <button className="chapter-reader-back" onClick={onClose}>
        ← 返回作品概览
      </button>
      <header className="chapter-reader-heading">
        <h2>
          第 {text(chapter.chapter_number)} 章 · {text(chapter.title)}
        </h2>
        <p>{text(chapter.word_count)} 字</p>
        <button
          className="secondary-button chapter-rewrite-button"
          onClick={onRewrite}
        >
          重写本章
        </button>
      </header>
      <div className="chapter-reader-body">
        <Text value={text(chapter.content)} />
      </div>
    </article>
  );
}
function CreateDialog({
  onClose,
  onSubmit,
  skills,
}: {
  onClose: () => void;
  onSubmit: (payload: Json) => void;
  skills: SkillOption[];
}) {
  const [data, setData] = useState(DEFAULT_BOOK);
  const [skillIds, setSkillIds] = useState<string[]>([]);
  const [characterRequirements, setCharacterRequirements] = useState("");
  const [stylePreferences, setStylePreferences] = useState("");
  const change = (key: string, value: unknown) =>
    setData((old) => ({ ...old, [key]: value }));
  const buildStoryIdea = () => {
    const supplements = [
      characterRequirements.trim() && `人物要求：${characterRequirements.trim()}`,
      stylePreferences.trim() && `风格与叙事偏好：${stylePreferences.trim()}`,
    ].filter(Boolean);
    return [text(data.premise).trim(), ...supplements].join("\n\n");
  };
  return (
    <dialog className="create-dialog" open>
      <form
        onSubmit={(event) => {
          event.preventDefault();
          onSubmit({
            ...data,
            premise: buildStoryIdea(),
            protagonist: "",
            central_conflict: "",
            tone: "",
            skill_ids: skillIds,
          });
        }}
      >
        <DialogHead kicker="新建故事" title="创建小说" onClose={onClose} />
        <div className="form-grid">
          <label className="full">
            故事想法
            <textarea
              value={text(data.premise)}
              onChange={(e) => change("premise", e.target.value)}
              placeholder="可以描述人物、背景或冲突"
              required
            />
          </label>
          <label>
            标题
            <input
              value={text(data.title)}
              onChange={(e) => change("title", e.target.value)}
              required
            />
          </label>
          <label>
            题材 / 类型
            <input
              value={text(data.genre)}
              onChange={(e) => change("genre", e.target.value)}
              required
            />
          </label>
          <label>
            预计章节数
            <input
              type="number"
              min="1"
              value={Number(data.target_chapters)}
              onChange={(e) => change("target_chapters", Number(e.target.value))}
            />
          </label>
          <label>
            每章目标字数
            <input
              type="number"
              min="1"
              value={Number(data.chapter_target_words)}
              onChange={(e) => change("chapter_target_words", Number(e.target.value))}
            />
          </label>
        </div>
        <details className="create-disclosure">
          <summary>补充创作要求（可选）</summary>
          <div className="form-grid create-disclosure-content">
            <label className="full">
              人物要求
              <textarea
                value={characterRequirements}
                onChange={(event) => setCharacterRequirements(event.target.value)}
              />
            </label>
            <label className="full">
              风格与叙事偏好
              <textarea
                value={stylePreferences}
                onChange={(event) => setStylePreferences(event.target.value)}
              />
            </label>
          </div>
        </details>
        {skills.length > 0 && (
          <details className="create-disclosure">
            <summary>高级配置</summary>
            <div className="create-skill-fields create-disclosure-content">
              <div className="create-skill-select">
                <RoundedSelect
                  label="添加创作 Skill"
                  value=""
                  disabled={skillIds.length >= 16 || skillIds.length === skills.length}
                  placeholder={skillIds.length === skills.length ? "已添加全部可用 Skill" : "添加创作 Skill（可选）"}
                  options={skills.filter((item) => !skillIds.includes(item.id)).map((item) => ({
                    value: item.id,
                    label: skillName(item, item.id),
                  }))}
                  onChange={(id) => {
                    if (id && !skillIds.includes(id)) setSkillIds((old) => [...old, id]);
                  }}
                />
              </div>
              {skillIds.length > 0 && (
                <div className="selected-skill-row create-selected-skills" aria-label="已选择的创作 Skill">
                  {skillIds.map((id) => (
                    <span key={id}>
                      {skillName(skills.find((item) => item.id === id), id)}
                      <button
                        type="button"
                        aria-label={`移除 ${skillName(skills.find((item) => item.id === id), id)}`}
                        onClick={() =>
                          setSkillIds((old) => old.filter((item) => item !== id))
                        }
                      >
                        ×
                      </button>
                    </span>
                  ))}
                </div>
              )}
            </div>
          </details>
        )}
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button className="primary-button">创建作品并生成基础资料</button>
        </div>
      </form>
    </dialog>
  );
}
function BatchDialog({ onClose, onSubmit }: any) {
  const [count, setCount] = useState(3),
    [auto, setAuto] = useState(false),
    [instruction, setInstruction] = useState("");
  return (
    <dialog className="create-dialog batch-dialog" open>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit({ chapter_count: count, auto_confirm: auto, instruction });
        }}
      >
        <DialogHead
          kicker="连续创作"
          title="连续创作"
          onClose={onClose}
        />
        <p className="dialog-description">
          每章都会基于最新已提交内容生成；遇到无法恢复的问题会自动暂停。
        </p>
        <div className="form-grid">
          <label>
            本次创作章节数
            <input
              type="number"
              min="2"
              max="12"
              value={count}
              onChange={(e) => setCount(Number(e.target.value))}
            />
          </label>
          <label className="full checkbox-field">
            <input
              type="checkbox"
              checked={auto}
              onChange={(e) => setAuto(e.target.checked)}
            />
            自动确认每章计划
            <span>
              计划通过后自动写作；未通过或发生异常时暂停。
            </span>
          </label>
          <label className="full">
            本批次要求（可选）
            <textarea
              value={instruction}
              onChange={(e) => setInstruction(e.target.value)}
            />
          </label>
        </div>
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button className="primary-button">开始连续创作</button>
        </div>
      </form>
    </dialog>
  );
}
function RewriteDialog({ project, chapter, onClose, onSubmit }: any) {
  const [instruction, setInstruction] = useState("");
  const number = Number(chapter?.chapter_number || 0);
  const last = Number(project?.last_committed_chapter || number);
  return (
    <dialog className="create-dialog rewrite-dialog" open>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit({ chapter_number: number, instruction });
        }}
      >
        <DialogHead
          kicker="重写章节"
          title={`重写第 ${number} 章`}
          onClose={onClose}
        />
        <div className="rewrite-impact">
          <strong>时间线将发生变化</strong>
          <p>
            {number === last
              ? `原第 ${number} 章会退出正史并保留在历史归档中。`
              : `原第 ${number}～${last} 章都会退出正史并保留在历史归档中。`}
          </p>
        </div>
        <label className="rewrite-instruction-label">
          本次重写要求
          <textarea
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            placeholder="例如：保留人物动机，但延后关键线索出现；减少技术说明，加强人物冲突。"
          />
          <span>可以留空。生成候选计划后，你仍然可以在聊天中继续调整。</span>
        </label>
        <p className="rewrite-notice">
          确认后会立即归档原时间线并生成新计划。之后取消候选计划不会自动恢复旧章节。
        </p>
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button className="primary-button danger-button">
            确认并生成计划
          </button>
        </div>
      </form>
    </dialog>
  );
}
function DialogHead({ title, onClose }: any) {
  return (
    <div className="dialog-heading">
      <div>
        <h2>{title}</h2>
      </div>
      <button type="button" className="icon-button" onClick={onClose}>
        ×
      </button>
    </div>
  );
}
function MemoryDrawer({ memories, bookId, onClose, onToggle, onDelete, onCorrect }: any) {
  const [scope, setScope] = useState("all"),
    [status, setStatus] = useState("active"),
    [correcting, setCorrecting] = useState<Json | null>(null),
    [correction, setCorrection] = useState("");
  const filtered = memories.filter(
    (item: Json) =>
      (status === "all" || text(item.status) === status) &&
      (scope === "all" || scope === text(item.scope_type)) &&
      (scope !== "book" || text(item.scope_id) === bookId),
  );
  return (
    <>
      <aside className="side-drawer open">
        <div className="drawer-heading">
          <div>
            <h2>会话记忆</h2>
          </div>
          <button className="icon-button" onClick={onClose}>
            ×
          </button>
        </div>
        <p className="drawer-description">
          系统自动保留跨会话的讨论偏好与未完成话题，只用于帮助理解对话；不会直接影响正文。需要约束后续创作，请使用“创作控制”。
        </p>
        <div className="drawer-filters">
          <RoundedSelect label="记忆作用域" value={scope} onChange={setScope} options={[
            { value: "all", label: "全部作用域" },
            { value: "global", label: "全局" },
            { value: "book", label: "当前作品" },
          ]} />
          <RoundedSelect label="记忆状态" value={status} onChange={setStatus} options={[
            { value: "active", label: "生效中" },
            { value: "all", label: "全部状态" },
            { value: "superseded", label: "已替代" },
            { value: "disabled", label: "已停用" },
          ]} />
        </div>
        <div className="memory-list">
          {filtered.length ? (
            filtered.map((item: Json) => (
              <article
                className={
                  text(item.status) === "active"
                    ? "memory-card"
                    : "memory-card muted-card"
                }
                key={text(item.memory_id)}
              >
                <div className="memory-card-heading">
                  <strong>{text(item.description)}</strong>
                  <span>{text(item.status)}</span>
                </div>
                <p>{text(item.content)}</p>
                <div className="memory-meta">
                  {text(item.memory_type)} · {text(item.scope_type)} · 重要性{" "}
                  {text(item.importance)}
                </div>
                <div className="memory-actions">
                  <button
                    onClick={() =>
                      void onToggle(
                        text(item.memory_id),
                        text(item.status) === "disabled",
                      )
                    }
                  >
                    {text(item.status) === "disabled" ? "恢复" : "停用"}
                  </button>
                  <button
                    onClick={() => {
                      setCorrecting(item);
                      setCorrection(text(item.content));
                    }}
                  >
                    更正
                  </button>
                  <button className="text-danger" onClick={() => void onDelete(text(item.memory_id))}>
                    删除
                  </button>
                </div>
              </article>
            ))
          ) : (
            <div className="empty-list">当前筛选条件下没有会话记忆</div>
          )}
        </div>
        {correcting && (
          <div className="memory-correction" role="dialog" aria-label="更正会话记忆">
            <strong>更正会话记忆</strong>
            <p>系统会保留原记录，并将其标为“已替代”。</p>
            <textarea
              value={correction}
              onChange={(event) => setCorrection(event.target.value)}
              placeholder="输入更正后的记忆"
            />
            <div className="memory-actions">
              <button onClick={() => { setCorrecting(null); setCorrection(""); }}>取消</button>
              <button
                className="primary-button"
                disabled={!correction.trim()}
                onClick={() => {
                  void onCorrect(text(correcting.memory_id), { content: correction.trim() });
                  setCorrecting(null);
                  setCorrection("");
                }}
              >
                保存更正
              </button>
            </div>
          </div>
        )}
      </aside>
    </>
  );
}
function CreativeControlDrawer({ control, onClose, onSave }: any) {
  const [data, setData] = useState(control);
  return (
    <aside className="side-drawer open">
      <form
        onSubmit={(event) => {
          event.preventDefault();
          void onSave({
            author_intent: text(data.author_intent),
            current_focus: text(data.current_focus),
            current_focus_mode:
              text(data.current_focus_mode) || "single_chapter",
          });
        }}
      >
        <div className="drawer-heading">
          <div>
            <h2>创作控制</h2>
          </div>
          <button type="button" className="icon-button" onClick={onClose}>
            ×
          </button>
        </div>
        <p className="drawer-description">为后续创作设定长期方向与当前重点。</p>
        <div className="control-fields">
          <label>
            长期创作方向
            <textarea
              value={text(data.author_intent)}
              onChange={(event) =>
                setData({ ...data, author_intent: event.target.value })
              }
              placeholder="例如：保持江湖悬疑感，不急于揭露真相。"
            />
          </label>
          <label>
            当前焦点（可选）
            <textarea
              value={text(data.current_focus)}
              onChange={(event) =>
                setData({ ...data, current_focus: event.target.value })
              }
              placeholder="例如：下一章推进师父秘密，但暂时不要揭穿。"
            />
          </label>
          <div className="focus-mode">
            <span>焦点有效期</span>
            <RoundedSelect
              label="焦点有效期"
              value={text(data.current_focus_mode) || "single_chapter"}
              onChange={(value) => setData({ ...data, current_focus_mode: value })}
              options={[
                { value: "single_chapter", label: "仅下一章成功提交前有效" },
                { value: "persistent", label: "持续生效，直到手动清除" },
              ]}
            />
          </div>
        </div>
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onClose}>
            取消
          </button>
          <button className="primary-button">保存</button>
        </div>
      </form>
    </aside>
  );
}
function TraceDrawer({ trace, onClose }: any) {
  const notes = Array.isArray(trace?.notes) ? trace.notes : [];
  const notedTokens = notes
    .map((item: unknown) =>
      String(item).match(/估算(?:使用)?\s*(\d+)\/(\d+)\s*Token/),
    )
    .find(Boolean);
  const estimated = Number(trace?.estimated_tokens ?? notedTokens?.[1] ?? 0);
  const budget = Number(trace?.budget ?? notedTokens?.[2] ?? 0);
  const skillResolution = obj(trace?.skill_resolution);
  const activatedSkills = list(skillResolution.activated_skills).map((item) => {
    const skill = obj(item);
    const hash = text(skill.content_hash);
    return `${text(skill.skill_id)}${hash ? ` · ${hash.slice(0, 12)}` : ""}`;
  });
  const rows = [
    ["预算", `${estimated}/${budget} Token`],
    ["已激活 Skill", activatedSkills],
    ["本次完整加载 Skill", list(skillResolution.materialized_ids).map(text)],
    ["仅加载 metadata", list(skillResolution.metadata_only_ids).map(text)],
    [
      "Skill Resolver",
      Object.keys(skillResolution).length
        ? `${text(skillResolution.strategy) || "未知"}${skillResolution.fallback === true ? " · fallback" : ""}`
        : "无",
    ],
    ["受保护来源", trace?.protected_source_ids || []],
    ["会话记忆", trace?.selected_memory_ids || []],
    ["压缩来源", trace?.compressed_source_ids || []],
    ["排除来源", trace?.excluded_source_ids || []],
    ["说明", notes],
  ];
  return (
    <aside className="side-drawer open">
      <div className="drawer-heading">
        <div>
          <h2>本轮上下文</h2>
        </div>
        <button className="icon-button" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="trace-content">
        {trace ? (
          rows.map(([label, value]) => (
            <section className="trace-section" key={label as string}>
              <h3>{label}</h3>
              {Array.isArray(value) ? (
                <ul>
                  {value.length ? (
                    value.map((item) => (
                      <li key={String(item)}>{String(item)}</li>
                    ))
                  ) : (
                    <li>无</li>
                  )}
                </ul>
              ) : (
                <p>{String(value)}</p>
              )}
            </section>
          ))
        ) : (
          <div className="empty-list">这条消息没有 Context Trace。</div>
        )}
      </div>
    </aside>
  );
}
function mergeTimeline(items: TimelineEvent[]) {
  const map = new Map(items.map((item) => [item.event_id, item]));
  return [...map.values()].sort((a, b) => a.sequence - b.sequence);
}
function errorMessage(error: unknown) {
  return error instanceof Error ? error.message : "请求失败";
}
