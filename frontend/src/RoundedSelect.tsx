import { useEffect, useRef, useState } from "react";

export interface SelectOption { value: string; label: string; }

export function RoundedSelect({
  label, value, options, onChange, disabled = false, placeholder = "请选择",
}: {
  label: string;
  value: string;
  options: SelectOption[];
  onChange: (value: string) => void;
  disabled?: boolean;
  placeholder?: string;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const optionRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const selectedIndex = options.findIndex((item) => item.value === value);

  useEffect(() => {
    if (!open) return;
    const focusId = window.requestAnimationFrame(() => optionRefs.current[Math.max(selectedIndex, 0)]?.focus());
    const closeOutside = (event: PointerEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener("pointerdown", closeOutside);
    return () => {
      window.cancelAnimationFrame(focusId);
      document.removeEventListener("pointerdown", closeOutside);
    };
  }, [open, selectedIndex]);

  const moveFocus = (direction: number) => {
    const current = optionRefs.current.findIndex((item) => item === document.activeElement);
    const next = (current + direction + options.length) % options.length;
    optionRefs.current[next]?.focus();
  };

  return <div className="rounded-select" ref={rootRef}>
    <button type="button" ref={triggerRef} className="rounded-select-trigger" aria-label={label} aria-haspopup="menu" aria-expanded={open} disabled={disabled || !options.length} onClick={() => setOpen((current) => !current)} onKeyDown={(event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        setOpen(true);
      }
    }}>
      <span>{selectedIndex >= 0 ? options[selectedIndex].label : placeholder}</span>
      <svg viewBox="0 0 16 16" aria-hidden="true"><path d="m4 6 4 4 4-4" /></svg>
    </button>
    {open && <div className="rounded-select-menu" role="menu" aria-label={label} onKeyDown={(event) => {
      if (event.key === "ArrowDown" || event.key === "ArrowUp") {
        event.preventDefault();
        moveFocus(event.key === "ArrowDown" ? 1 : -1);
      } else if (event.key === "Home" || event.key === "End") {
        event.preventDefault();
        optionRefs.current[event.key === "Home" ? 0 : options.length - 1]?.focus();
      } else if (event.key === "Escape") {
        event.preventDefault();
        setOpen(false);
        triggerRef.current?.focus();
      } else if (event.key === "Tab") {
        setOpen(false);
      }
    }}>
      {options.map((item, index) => <button type="button" key={item.value} ref={(node) => { optionRefs.current[index] = node; }} className="rounded-select-option" role="menuitemradio" aria-checked={item.value === value} onClick={() => {
        onChange(item.value);
        setOpen(false);
        triggerRef.current?.focus();
      }}>{item.label}{item.value === value && <span aria-hidden="true">✓</span>}</button>)}
    </div>}
  </div>;
}
