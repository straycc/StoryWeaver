import Markdown, { type Components } from "react-markdown";
import remarkGfm from "remark-gfm";

// 来源链接一律新标签页打开，避免把用户正在写的会话页面顶掉。
// 正文引用是「[3](url)」形式（后端按登记表回填），渲染成小角标；
// 兼容旧数据的「[站点域名](url)」形式，同样按小标记处理。
const components: Components = {
  a: ({ node: _node, children, className, ...props }) => {
    const label = Array.isArray(children) ? children.join("") : String(children ?? "");
    const mark = /^\d{1,2}$/.test(label.trim());
    const citation =
      /^来源\s*\d+$/.test(label) || /^[a-z0-9-]+(\.[a-z0-9-]+)+$/i.test(label);
    const merged = [className, citation ? "citation-link" : ""].filter(Boolean).join(" ");
    if (mark) {
      return (
        <sup className="citation-mark">
          <a {...props} target="_blank" rel="noreferrer noopener">
            {children}
          </a>
        </sup>
      );
    }
    return (
      <a
        {...props}
        className={merged || undefined}
        target="_blank"
        rel="noreferrer noopener"
      >
        {children}
      </a>
    );
  },
};

export function MarkdownText({ value }: { value: string }) {
  return (
    <div className="assistant-markdown">
      <Markdown remarkPlugins={[remarkGfm]} components={components}>
        {value}
      </Markdown>
    </div>
  );
}
