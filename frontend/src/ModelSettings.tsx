import { useEffect, useState } from "react";
import { api, type ModelConfig, type ModelProvider, type ModelSelection } from "./api";
import { RoundedSelect } from "./RoundedSelect";

type Draft = {
  id: string;
  name: string;
  base_url: string;
  models: string;
  api_key: string;
  clear_key: boolean;
};

const emptyDraft = (): Draft => ({
  id: "", name: "", base_url: "", models: "", api_key: "", clear_key: false,
});

const providerDraft = (provider: ModelProvider): Draft => ({
  id: provider.id,
  name: provider.name,
  base_url: provider.base_url,
  models: provider.models.join("\n"),
  api_key: "",
  clear_key: false,
});

export function ModelSettings({ onClose, onChanged }: { onClose: () => void; onChanged: () => void }) {
  const [config, setConfig] = useState<ModelConfig | null>(null);
  const [activeId, setActiveId] = useState("");
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [editing, setEditing] = useState(false);
  const [changingKey, setChangingKey] = useState(false);
  const [selectedModel, setSelectedModel] = useState("");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    void api.modelConfig().then((value) => {
      setConfig(value);
      const provider = value.providers.find((item) => item.id === value.default_provider) || value.providers[0];
      if (provider) {
        setActiveId(provider.id);
        setDraft(providerDraft(provider));
        setSelectedModel(provider.id === value.default_provider && value.default_model && provider.models.includes(value.default_model)
          ? value.default_model : provider.models[0]);
      } else {
        setEditing(true);
      }
    }).catch((error) => setMessage(error instanceof Error ? error.message : String(error)));
  }, []);

  const provider = config?.providers.find((item) => item.id === activeId);
  const models = draft.models.split(/[\n,]/).map((item) => item.trim()).filter(Boolean);
  const isDefault = activeId === config?.default_provider && selectedModel === config?.default_model;

  const chooseProvider = (item: ModelProvider) => {
    setActiveId(item.id);
    setDraft(providerDraft(item));
    setSelectedModel(item.id === config?.default_provider && config.default_model && item.models.includes(config.default_model)
      ? config.default_model : item.models[0]);
    setEditing(false);
    setChangingKey(false);
    setMessage("");
  };

  const addProvider = () => {
    setActiveId("");
    setDraft(emptyDraft());
    setSelectedModel("");
    setEditing(true);
    setChangingKey(true);
    setMessage("");
  };

  const run = async (operation: () => Promise<ModelConfig | { ok: boolean }>, success: string) => {
    setBusy(true);
    setMessage("");
    try {
      const result = await operation();
      if ("providers" in result) {
        setConfig(result);
        onChanged();
      }
      setMessage(success);
      return result;
    } catch (error) {
      setMessage(error instanceof Error ? error.message : String(error));
      return null;
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    const result = await run(() => api.saveModelProvider({
      id: draft.id.trim(),
      name: draft.name.trim(),
      base_url: draft.base_url.trim(),
      models,
      ...(draft.api_key ? { api_key: draft.api_key } : {}),
      clear_key: draft.clear_key,
    }), "配置已保存");
    if (!result || !("providers" in result)) return;
    const saved = result.providers.find((item) => item.id === draft.id.trim());
    if (!saved) return;
    setActiveId(saved.id);
    setDraft(providerDraft(saved));
    setSelectedModel(saved.id === result.default_provider && result.default_model && saved.models.includes(result.default_model)
      ? result.default_model : saved.models[0]);
    setChangingKey(false);
    setEditing(false);
  };

  const selectDefault = async () => {
    if (!provider) return;
    const selection: ModelSelection = { provider_id: provider.id, model_id: selectedModel };
    await run(() => api.setDefaultModel(selection), "已设为默认模型");
  };

  return <div className="model-settings-overlay" role="presentation" onMouseDown={(event) => { if (event.target === event.currentTarget) onClose(); }}>
    <section className="model-settings-dialog" role="dialog" aria-modal="true" aria-label="模型设置">
      <header className="model-settings-header">
        <div><h2>模型</h2><p>添加 API 密钥后即可使用模型</p></div>
        <button type="button" className="model-settings-close" aria-label="关闭模型设置" onClick={onClose}>×</button>
      </header>

      <div className="model-settings-toolbar">
        <div className="model-settings-provider-select"><span>供应商</span>
          <RoundedSelect label="供应商" value={activeId} options={config?.providers.map((item) => ({ value: item.id, label: item.name })) || []} onChange={(value) => {
            const item = config?.providers.find((entry) => entry.id === value);
            if (item) chooseProvider(item);
          }} disabled={!config?.providers.length || busy} placeholder="选择供应商" />
        </div>
        <button type="button" className="model-settings-add" onClick={addProvider} disabled={busy || !config}>＋ 添加</button>
      </div>

      {!editing && provider && <div className="model-settings-card">
        <div className="model-settings-card-head">
          <div><strong>{provider.name}</strong><span className="model-settings-ready" aria-label={provider.has_api_key ? "密钥已配置" : "密钥未配置"} /></div>
          <button type="button" onClick={() => setEditing(true)}>编辑</button>
        </div>
        <div className="model-settings-field"><span>模型</span>
          <RoundedSelect label="模型" value={selectedModel} options={provider.models.map((item) => ({ value: item, label: item }))} onChange={setSelectedModel} disabled={busy} />
        </div>
        <div className="model-settings-key-row"><span>API Key</span><strong>{provider.has_api_key ? "••••••••••••••••" : "未配置"}</strong></div>
        <div className="model-settings-card-actions">
          {!isDefault && <button type="button" disabled={busy || !selectedModel} onClick={() => void selectDefault()}>设为默认</button>}
          <button type="button" disabled={busy || !selectedModel} onClick={() => void run(() => api.testModel({ provider_id: provider.id, model_id: selectedModel }), "连接成功")}>测试连接</button>
        </div>
      </div>}

      {editing && <div className="model-settings-editor">
        <div className="model-settings-editor-head"><strong>{provider ? `编辑 ${provider.name}` : "添加供应商"}</strong>
          {provider && <button type="button" onClick={() => chooseProvider(provider)}>取消</button>}</div>
        {!provider && <div className="model-settings-two-fields">
          <label className="model-settings-field">供应商 ID<input value={draft.id} onChange={(event) => setDraft({ ...draft, id: event.target.value })} placeholder="例如 deepseek" /></label>
          <label className="model-settings-field">显示名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} placeholder="例如 深度求索" /></label>
        </div>}

        <label className="model-settings-field">API Key
          {provider?.has_api_key && !changingKey && !draft.clear_key
            ? <div className="model-settings-masked"><span>••••••••••••••••</span><button type="button" onClick={() => setChangingKey(true)}>更换</button></div>
            : <input type="password" autoComplete="new-password" value={draft.api_key} onChange={(event) => setDraft({ ...draft, api_key: event.target.value, clear_key: false })} placeholder="输入 API Key；本地模型可留空" />}
        </label>
        {provider?.has_api_key && <button type="button" className="model-settings-text-button" onClick={() => {
          setDraft({ ...draft, api_key: "", clear_key: !draft.clear_key });
          setChangingKey(!draft.clear_key);
        }}>{draft.clear_key ? "撤销移除密钥" : "移除已保存的密钥"}</button>}
        {draft.clear_key && <small className="model-settings-note">保存后移除密钥</small>}

        <details className="model-settings-advanced" open={!provider}>
          <summary>{provider ? "自定义设置" : "模型接口"}</summary>
          {provider && <label className="model-settings-field">显示名称<input value={draft.name} onChange={(event) => setDraft({ ...draft, name: event.target.value })} /></label>}
          <label className="model-settings-field">API 地址<input value={draft.base_url} onChange={(event) => setDraft({ ...draft, base_url: event.target.value })} placeholder="https://api.example.com/v1" /></label>
          <label className="model-settings-field">模型 ID<textarea rows={3} value={draft.models} onChange={(event) => setDraft({ ...draft, models: event.target.value })} placeholder="每行一个模型 ID" /></label>
          {provider && <button type="button" className="model-settings-delete" disabled={busy} onClick={() => {
            if (!window.confirm(`删除供应商「${provider.name}」及其密钥？`)) return;
            void run(() => api.deleteModelProvider(provider.id), "供应商已删除").then((result) => {
              if (!result || !("providers" in result)) return;
              const next = result.providers.find((item) => item.id === result.default_provider) || result.providers[0];
              if (next) chooseProvider(next);
              else addProvider();
            });
          }}>删除供应商</button>}
        </details>
        <div className="model-settings-save-row"><button type="button" className="model-settings-primary" disabled={busy || !draft.id.trim() || !draft.name.trim() || !draft.base_url.trim() || !models.length} onClick={() => void save()}>{busy ? "保存中…" : "保存"}</button></div>
      </div>}
      {message && <p className="model-settings-message" role="status">{message}</p>}
    </section>
  </div>;
}
