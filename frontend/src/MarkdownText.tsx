import Markdown from "react-markdown";
import remarkGfm from "remark-gfm";

export function MarkdownText({ value }: { value: string }) {
  return <div className="assistant-markdown"><Markdown remarkPlugins={[remarkGfm]}>{value}</Markdown></div>;
}
