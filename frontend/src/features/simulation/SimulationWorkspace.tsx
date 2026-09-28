import { useCallback, useEffect, useMemo, useState } from "react";
import type { FormEvent } from "react";
import {
  api,
  type Json,
  type Simulation,
  type SimulationTurn,
} from "../../api";
import { RoundedSelect } from "../../RoundedSelect";

const text = (value: unknown) =>
  typeof value === "string" ? value : value == null ? "" : String(value);
const json = (value: unknown): Json =>
  value && typeof value === "object" ? (value as Json) : {};
const requestId = () => crypto.randomUUID();

/** 角色剧场嵌入既有工作台，避免使用另一套路由外壳。 */
export function SimulationWorkspace({
  bookId,
  onError,
}: {
  bookId: string | null;
  onError: (message: string) => void;
}) {
  const [screen, setScreen] = useState<"list" | "create" | "stage">("list");
  const [items, setItems] = useState<Simulation[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const refresh = useCallback(async () => {
    if (!bookId) {
      setItems([]);
      return;
    }
    try {
      setItems((await api.simulations(bookId)).simulations);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  }, [bookId, onError]);

  useEffect(() => {
    setScreen("list");
    setSelectedId(null);
    void refresh();
  }, [bookId, refresh]);
  if (!bookId)
    return (
      <div className="simulation-workspace simulation-empty-workspace">
        <h1>角色剧场</h1>
        <p>请先在左侧选择或绑定一部作品，再创建非正史模拟。</p>
      </div>
    );
  if (screen === "create")
    return (
      <SimulationCreate
        bookId={bookId}
        onCancel={() => setScreen("list")}
        onCreated={(id) => {
          setSelectedId(id);
          setScreen("stage");
          void refresh();
        }}
        onError={onError}
      />
    );
  if (screen === "stage" && selectedId)
    return (
      <SimulationStage
        simulationId={selectedId}
        onBack={() => {
          setScreen("list");
          void refresh();
        }}
        onError={onError}
      />
    );
  return (
    <div className="simulation-workspace">
      <header className="simulation-workspace-header">
        <div>
          <h1>角色剧场</h1>
          <p>以当前作品的章节状态为基线，探索不写回正史的角色选择与冲突。</p>
        </div>
        <button className="primary-button" onClick={() => setScreen("create")}>
          创建模拟
        </button>
      </header>
      <div className="simulation-list">
        {items.length ? (
          items.map((item) => (
            <button
              className="simulation-list-item"
              key={item.simulation_id}
              onClick={() => {
                setSelectedId(item.simulation_id);
                setScreen("stage");
              }}
            >
              <strong>{item.current_location || "未命名场景"}</strong>
              <span>
                第 {item.base_chapter_number} 章基线 ·{" "}
                {item.mode === "roleplay" ? "扮演" : "旁观"} · 已进行{" "}
                {item.current_turn} 回合
              </span>
              <em>{statusLabel(item.status)}</em>
            </button>
          ))
        ) : (
          <div className="simulation-empty">
            <strong>还没有角色剧场</strong>
            <p>创建一个非正史场景，试探人物的选择、秘密与冲突。</p>
          </div>
        )}
      </div>
    </div>
  );
}

function SimulationCreate({
  bookId,
  onCancel,
  onCreated,
  onError,
}: {
  bookId: string;
  onCancel: () => void;
  onCreated: (id: string) => void;
  onError: (message: string) => void;
}) {
  const [book, setBook] = useState<Json>({});
  const [mode, setMode] = useState("observer");
  const [selected, setSelected] = useState<string[]>([]);
  const [actor, setActor] = useState("");
  const [customDraft, setCustomDraft] = useState({
    name: "",
    role: "",
    goal: "",
    secret: "",
    user: false,
  });
  const [customCharacters, setCustomCharacters] = useState<Json[]>([]);
  const [openingMode, setOpeningMode] = useState<"inherit" | "sandbox">(
    "inherit",
  );
  const [form, setForm] = useState({
    base_chapter_number: 1,
    location: "",
    opening_direction: "",
  });
  // 此处必须读取完整作品接口；旧兼容接口只有展示摘要，没有 foundation.characters。
  useEffect(() => {
    void api
      .getBook(bookId)
      .then((value) => {
        setBook(value);
        const state = json(value.state);
        const last = Number(state.last_committed_chapter || 0);
        setForm((old) => ({
          ...old,
          base_chapter_number: last || 1,
          location: text(state.current_location),
        }));
      })
      .catch((error) =>
        onError(error instanceof Error ? error.message : String(error)),
      );
  }, [bookId, onError]);
  const profiles = useMemo(
    () =>
      Array.isArray(json(book.foundation).characters)
        ? (json(book.foundation).characters as Json[])
        : [],
    [book],
  );
  const chapters = useMemo(
    () => (Array.isArray(book.chapters) ? (book.chapters as Json[]) : []),
    [book],
  );
  const participants = useMemo(
    () => [
      ...profiles.filter((profile) =>
        selected.includes(text(profile.character_id)),
      ),
      ...customCharacters,
    ],
    [customCharacters, profiles, selected],
  );
  const addCustom = () => {
    if (!customDraft.name.trim()) {
      onError("请先填写沙盒人物名称。");
      return;
    }
    if (selected.length + customCharacters.length >= 6) {
      onError("参与人物最多 6 名。");
      return;
    }
    const item = {
      character_id: `sandbox-${crypto.randomUUID().slice(0, 10)}`,
      name: customDraft.name.trim(),
      role: customDraft.role.trim() || "未知身份",
      goal: customDraft.goal.trim() || "探索当前场景",
      secret: customDraft.secret.trim(),
      public_profile: customDraft.role.trim() || "未知身份",
      origin: customDraft.user ? "user_created" : "sandbox_npc",
    };
    setCustomCharacters((old) => [...old, item]);
    setCustomDraft({ name: "", role: "", goal: "", secret: "", user: false });
  };
  const submit = async (event: FormEvent) => {
    event.preventDefault();
    if (!participants.length) {
      onError("请至少选择或添加一名参与人物。");
      return;
    }
    if (mode === "roleplay" && !actor) {
      onError("扮演模式需要选择你扮演的人物。");
      return;
    }
    try {
      const simulation = await api.createSimulation(bookId, {
        ...form,
        location: openingMode === "inherit" ? "" : form.location,
        mode,
        user_character_id: mode === "roleplay" ? actor : null,
        canonical_character_ids: selected,
        custom_characters: customCharacters,
      });
      onCreated(simulation.simulation_id);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="simulation-workspace">
      <header className="simulation-workspace-header">
        <div>
          <h1>创建模拟</h1>
          <p>模拟是非正史沙盒，不会改动章节、伏笔、人物档案或会话记忆。</p>
        </div>
        <button className="secondary-button" onClick={onCancel}>
          返回列表
        </button>
      </header>
      <form className="simulation-form embedded" onSubmit={submit}>
        <div className="simulation-grid">
          <div className="simulation-select-field">
            <span>基准章节</span>
            <RoundedSelect label="基准章节" value={String(form.base_chapter_number)} onChange={(value) =>
              setForm({ ...form, base_chapter_number: Number(value) })
            } options={chapters.map((chapter) => ({
              value: text(chapter.chapter_number),
              label: `第 ${text(chapter.chapter_number)} 章 · ${text(chapter.title)}`,
            }))} />
          </div>
          <div className="simulation-select-field">
            <span>互动方式</span>
            <RoundedSelect label="互动方式" value={mode} onChange={setMode} options={[
              { value: "observer", label: "旁观推演" },
              { value: "roleplay", label: "角色扮演" },
            ]} />
          </div>
        </div>
        <fieldset>
          <legend>
            参与人物（已选 {selected.length + customCharacters.length}/6）
          </legend>
          <div className="character-choice-grid">
            {profiles.map((profile) => {
              const id = text(profile.character_id);
              const checked = selected.includes(id);
              return (
                <label className="check-row" key={id}>
                  <input
                    type="checkbox"
                    disabled={
                      !checked && selected.length + customCharacters.length >= 6
                    }
                    checked={checked}
                    onChange={(e) =>
                      setSelected(
                        e.target.checked
                          ? [...selected, id]
                          : selected.filter((value) => value !== id),
                      )
                    }
                  />
                  {text(profile.name)} <small>{text(profile.role)}</small>
                </label>
              );
            })}
          </div>
        </fieldset>
        {mode === "roleplay" && (
          <div className="simulation-select-field">
            <span>你扮演</span>
            <RoundedSelect label="你扮演" value={actor} onChange={setActor} options={[
              { value: "", label: "请选择参与人物" },
              ...participants.map((character) => ({
                value: text(character.character_id),
                label: text(character.name),
              })),
            ]} />
          </div>
        )}
        <fieldset>
          <legend>可选：新增沙盒人物（可添加多名）</legend>
          <div className="simulation-grid">
            <input
              placeholder="名称"
              value={customDraft.name}
              onChange={(e) =>
                setCustomDraft({ ...customDraft, name: e.target.value })
              }
            />
            <input
              placeholder="身份"
              value={customDraft.role}
              onChange={(e) =>
                setCustomDraft({ ...customDraft, role: e.target.value })
              }
            />
            <input
              placeholder="目标"
              value={customDraft.goal}
              onChange={(e) =>
                setCustomDraft({ ...customDraft, goal: e.target.value })
              }
            />
            <label className="check-row">
              <input
                type="checkbox"
                checked={customDraft.user}
                onChange={(e) =>
                  setCustomDraft({ ...customDraft, user: e.target.checked })
                }
              />
              作为用户创建人物
            </label>
          </div>
          <textarea
            placeholder="秘密（仅供剧场 Agent 使用）"
            value={customDraft.secret}
            onChange={(e) =>
              setCustomDraft({ ...customDraft, secret: e.target.value })
            }
          />
          <button
            type="button"
            className="secondary-button"
            onClick={addCustom}
          >
            添加人物
          </button>
          {customCharacters.length > 0 && (
            <div className="sandbox-character-list">
              {customCharacters.map((character) => (
                <span key={text(character.character_id)}>
                  {text(character.name)} · {text(character.role)}
                  <button
                    type="button"
                    onClick={() => {
                      const id = text(character.character_id);
                      setCustomCharacters((old) =>
                        old.filter((item) => text(item.character_id) !== id),
                      );
                      if (actor === id) setActor("");
                    }}
                  >
                    ×
                  </button>
                </span>
              ))}
            </div>
          )}
        </fieldset>
        <fieldset className="simulation-opening">
          <legend>开场方式</legend>
          <label className="check-row">
            <input
              type="radio"
              checked={openingMode === "inherit"}
              onChange={() => setOpeningMode("inherit")}
            />
            延续第 {form.base_chapter_number} 章现场
          </label>
          <label className="check-row">
            <input
              type="radio"
              checked={openingMode === "sandbox"}
              onChange={() => setOpeningMode("sandbox")}
            />
            创建新的沙盒场景
          </label>
          {openingMode === "sandbox" && (
            <label>
              场景地点
              <input
                placeholder="例如：山脚客栈"
                value={form.location}
                onChange={(e) => setForm({ ...form, location: e.target.value })}
              />
            </label>
          )}
          <label>
            开场说明（可选）
            <textarea
              placeholder="例如：三人在客栈继续讨论密令；林秀有所怀疑，但暂时不要揭露真相。"
              value={form.opening_direction}
              onChange={(e) =>
                setForm({ ...form, opening_direction: e.target.value })
              }
            />
          </label>
          <p>
            场景内人物默认全部出现在当前舞台；这是非正史前提，不改变正式作品状态。
          </p>
        </fieldset>
        <div className="dialog-actions">
          <button type="button" className="secondary-button" onClick={onCancel}>
            取消
          </button>
          <button className="primary-button" type="submit">
            开始角色剧场
          </button>
        </div>
      </form>
    </div>
  );
}

function SimulationStage({
  simulationId,
  onBack,
  onError,
}: {
  simulationId: string;
  onBack: () => void;
  onError: (message: string) => void;
}) {
  const [simulation, setSimulation] = useState<Simulation | null>(null);
  const [turns, setTurns] = useState<SimulationTurn[]>([]);
  const [input, setInput] = useState("");
  const [targetCharacterId, setTargetCharacterId] = useState("");
  const [previewBlocks, setPreviewBlocks] = useState<Json[]>([]);
  const [busy, setBusy] = useState(false);
  const [pendingInput, setPendingInput] = useState("");
  const [runError, setRunError] = useState("");
  const [lifecycleNote, setLifecycleNote] = useState("");
  const load = useCallback(async () => {
    try {
      const [item, data] = await Promise.all([
        api.simulation(simulationId),
        api.simulationTurns(simulationId),
      ]);
      setSimulation(item);
      setTurns(data.turns);
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  }, [onError, simulationId]);
  useEffect(() => {
    void load();
  }, [load]);
  const watch = (jobId: string) => {
    const source = api.streamJob(jobId, (event) => {
      if (event.event_type === "simulation_character_preview") {
        const block = event.payload.block;
        if (block && typeof block === "object")
          setPreviewBlocks((old) => [...old, block as Json]);
      }
      if (
        ["simulation_turn_committed", "job_succeeded", "job_failed"].includes(
          event.event_type,
        )
      )
        void load();
      if (event.event_type === "simulation_turn_committed")
        setPreviewBlocks([]);
      if (
        event.event_type === "simulation_turn_failed" ||
        event.event_type === "job_failed"
      ) {
        setPreviewBlocks([]);
        const message = text(event.payload.error) || "本回合推演失败，请重试。";
        setRunError(message);
        onError(message);
      }
      if (
        [
          "job_succeeded",
          "job_failed",
          "job_paused",
          "job_interrupted",
        ].includes(event.event_type)
      ) {
        source.close();
        setBusy(false);
        setPendingInput("");
      }
    });
  };
  const send = async (automatic = false) => {
    if (!simulation || busy) return;
    if (!automatic && !input.trim()) return;
    const content = automatic ? "继续推演当前场景" : input.trim();
    setBusy(true);
    setRunError("");
    setPreviewBlocks([]);
    setPendingInput(content);
    try {
      const body = {
        client_request_id: requestId(),
        expected_version: simulation.version,
        input_type:
          simulation.mode === "observer" ? "director_event" : "speech_action",
        content,
        target_character_id:
          !automatic && simulation.mode === "roleplay" && targetCharacterId
            ? targetCharacterId
            : null,
      };
      const result = automatic
        ? await api.continueSimulation(simulationId, body)
        : await api.submitSimulationTurn(simulationId, body);
      setInput("");
      setTargetCharacterId("");
      watch(result.job_id);
    } catch (error) {
      setBusy(false);
      setPendingInput("");
      const message = error instanceof Error ? error.message : String(error);
      setRunError(message);
      onError(message);
    }
  };
  const updateMode = async (mode: string, userCharacterId: string | null) => {
    if (!simulation || simulation.status !== "active") return;
    try {
      await api.updateSimulationMode(simulationId, {
        expected_version: simulation.version,
        mode,
        user_character_id: userCharacterId,
      });
      await load();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  };
  if (!simulation)
    return (
      <div className="simulation-workspace simulation-empty-workspace">
        正在读取角色剧场…
      </div>
    );
  const characterName = (id: string) =>
    text(
      simulation.characters.find((item) => text(item.character_id) === id)
        ?.name,
    ) || id;
  const presentCharacters = simulation.characters.filter((item) =>
    Boolean(item.present),
  );
  const isActive = simulation.status === "active";
  const isPaused = simulation.status === "paused";
  const isCompleted = simulation.status === "completed";
  const changeLifecycle = async (operation: "pause" | "resume" | "finish") => {
    if (
      operation === "finish" &&
      !window.confirm("结束后不能恢复或继续推演；本次非正史记录会保留。确定结束吗？")
    ) return;
    try {
      const updated = await api.simulationOperation(simulationId, operation);
      setLifecycleNote(
        operation === "pause"
          ? "剧场已暂停，恢复后可以继续互动。"
          : operation === "resume"
            ? "剧场已恢复，可以继续互动。"
            : "剧场已结束并封存，不会影响正式作品。",
      );
      setSimulation(updated);
      await load();
    } catch (error) {
      onError(error instanceof Error ? error.message : String(error));
    }
  };
  return (
    <div className="simulation-workspace simulation-stage">
      <header className="simulation-workspace-header">
        <div>
          <button className="back-link" onClick={onBack}>
            ← 返回角色剧场
          </button>
          <h1>{simulation.current_location || "角色剧场"}</h1>
          <p className="simulation-base-note">非正史 · 第 {simulation.base_chapter_number} 章基线</p>
          <p>{simulation.scene_summary}</p>
        </div>
        <div className="simulation-stage-actions">
          <span className={`simulation-status status-${simulation.status}`}>
            {statusLabel(simulation.status)}
          </span>
          <RoundedSelect label="互动方式" value={simulation.mode} disabled={!isActive} onChange={(value) =>
            void updateMode(value, value === "roleplay"
              ? simulation.user_character_id || text(presentCharacters[0]?.character_id)
              : null)
          } options={[
            { value: "observer", label: "旁观" },
            { value: "roleplay", label: "扮演" },
          ]} />
          {simulation.mode === "roleplay" && (
            <RoundedSelect label="扮演人物" value={simulation.user_character_id || ""} disabled={!isActive}
              onChange={(value) => void updateMode("roleplay", value)}
              options={presentCharacters.map((item) => ({
                value: text(item.character_id), label: text(item.name),
              }))} />
          )}
          {!isCompleted && (
            <button className="secondary-button" onClick={() => void changeLifecycle(isActive ? "pause" : "resume")}>
              {isActive ? "暂停" : "恢复"}
            </button>
          )}
          {!isCompleted && (
            <button className="danger-button" onClick={() => void changeLifecycle("finish")}>
              结束剧场
            </button>
          )}
        </div>
      </header>
      <div className="world-state">
        <span>时间：{simulation.current_time}</span>
        {simulation.active_event && (
          <span>事件：{simulation.active_event}</span>
        )}
        <span>
          在场：{presentCharacters.map((item) => text(item.name)).join("、")}
        </span>
      </div>
      {(isPaused || isCompleted || lifecycleNote) && (
        <div className={`simulation-lifecycle-note ${isCompleted ? "completed" : ""}`}>
          {lifecycleNote || (isPaused
            ? "剧场已暂停；点击“恢复”后可继续互动。"
            : "剧场已结束并封存；可返回列表查看记录，或创建新的非正史场景。")}
        </div>
      )}
      <div className="roleplay-stream">
        {turns.some((turn) => turn.status === "completed" && turn.output) ? (() => {
          let observerRound = 0;
          return turns.filter((turn) => turn.status === "completed" && turn.output).flatMap((turn) => {
            const blocks = Array.isArray(turn.output?.blocks)
              ? (turn.output.blocks as Json[])
              : [];
            const ownInput =
              turn.input_type !== "observer_continue" && turn.user_input ? (
                <div
                  className={
                    turn.input_type === "director_event"
                      ? "director-block"
                      : "user-block"
                  }
                  key={`${turn.turn_id}-input`}
                >
                  {turn.user_input}
                </div>
              ) : null;
            return [
              turn.input_type === "observer_continue" ? (() => {
                observerRound += 1;
                return (
                <div className="simulation-turn-marker" key={`${turn.turn_id}-marker`}>
                  场景推演 · 第 {observerRound} 回合
                </div>
                );
              })() : null,
              ownInput,
              ...blocks.map((block, index) =>
                renderBlock(
                  block,
                  `${turn.turn_id}-${index}`,
                  simulation.user_character_id,
                  characterName,
                ),
              ),
            ];
          });
        })() : (
          <div className="simulation-empty">
            场景已经建立。输入角色行动，或让系统继续旁观推演。
          </div>
        )}
        {previewBlocks.map((block, index) => (
          <div className="simulation-preview" key={`preview-${index}`}>
            {renderBlock(
              block,
              `preview-block-${index}`,
              simulation.user_character_id,
              characterName,
            )}
            <small>生成中，尚未提交</small>
          </div>
        ))}
        {pendingInput && (
          <div className="simulation-pending">
            正在推演…
          </div>
        )}
        {runError && (
          <div className="simulation-run-error">本回合未提交：{runError}</div>
        )}
      </div>
      {!isCompleted && <div className="simulation-composer">
        <div className="simulation-composer-controls">
          {simulation.mode === "observer" && (
            <button
              className="secondary-button simulation-continue"
              disabled={busy || !isActive}
              onClick={() => void send(true)}
            >
              继续推演 3 回合
            </button>
          )}
          {simulation.mode === "roleplay" && (
            <div className="target-character">
              <span>对谁说</span>
              <RoundedSelect label="对谁说" value={targetCharacterId} disabled={busy || !isActive}
                onChange={setTargetCharacterId}
                options={[
                  { value: "", label: "交给场景决定" },
                  ...presentCharacters.filter((item) => text(item.character_id) !== simulation.user_character_id)
                    .map((item) => ({ value: text(item.character_id), label: text(item.name) })),
                ]} />
            </div>
          )}
        </div>
        <div className="simulation-input-shell">
          <textarea
            disabled={busy || !isActive}
            placeholder={
              simulation.mode === "roleplay"
                ? "输入你的对白或行动…"
                : "输入场景指令或外部事件…"
            }
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="simulation-input-footer">
            <button
              className="simulation-send-button"
              aria-label={
                simulation.mode === "observer" ? "发送场景指令" : "发送角色行动"
              }
              disabled={!input.trim() || busy || !isActive}
              onClick={() => void send()}
            >
              ↑
            </button>
          </div>
        </div>
      </div>}
    </div>
  );
}

function statusLabel(status: string) {
  return (
    (
      { active: "进行中", paused: "已暂停", completed: "已结束" } as Record<
        string,
        string
      >
    )[status] || status
  );
}

function renderBlock(
  block: Json,
  key: string,
  userCharacterId: string | null,
  characterName: (id: string) => string,
) {
  const speaker = text(block.speaker_id);
  if (text(block.type) === "narration")
    return (
      <div className="narration-block" key={key}>
        {text(block.content)}
      </div>
    );
  const action = text(block.action);
  const content = text(block.content);
  const bubble = content ? (
    <div className={speaker === userCharacterId ? "user-block" : "npc-block"}>
      <span className="character-avatar">
        {characterName(speaker).slice(0, 1) || "?"}
      </span>
      <div>
        <strong>{characterName(speaker)}：</strong>
        <span>{content}</span>
      </div>
    </div>
  ) : null;
  return (
    <div className="character-contribution" key={key}>
      {action && (
        <div className="character-action">
          {characterName(speaker)} {action}
        </div>
      )}
      {bubble}
    </div>
  );
}
