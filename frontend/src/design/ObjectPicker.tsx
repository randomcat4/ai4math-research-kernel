import { type FormEvent, useId } from "react";
import "./object-picker.css";

export interface ObjectPickerOption {
  id: string;
  label: string;
  description?: string;
  meta?: string;
  disabled?: boolean;
}

interface SearchControl {
  value: string;
  placeholder: string;
  onChange(value: string): void;
  onSubmit(): void;
}

export interface ObjectPickerProps {
  label: string;
  description: string;
  options: ObjectPickerOption[];
  selectedIds: string[];
  onChange(ids: string[]): void;
  multiple?: boolean;
  status?: "READY" | "LOADING" | "NOT_PUBLISHED" | "ERROR";
  statusText?: string;
  emptyText?: string;
  search?: SearchControl;
}

export function ObjectPicker({
  label,
  description,
  options,
  selectedIds,
  onChange,
  multiple = false,
  status = "READY",
  statusText,
  emptyText = "当前研究中没有可选择的对象。",
  search,
}: ObjectPickerProps) {
  const descriptionId = useId();
  const selected = new Set(selectedIds);

  function toggle(option: ObjectPickerOption) {
    if (option.disabled) return;
    if (!multiple) {
      onChange(selected.has(option.id) ? [] : [option.id]);
      return;
    }
    onChange(
      selected.has(option.id)
        ? selectedIds.filter((id) => id !== option.id)
        : [...selectedIds, option.id],
    );
  }

  function submitSearch(event: FormEvent) {
    event.preventDefault();
    search?.onSubmit();
  }

  return (
    <fieldset className="rk-object-picker" aria-describedby={descriptionId}>
      <legend>{label}</legend>
      <p id={descriptionId}>{description}</p>
      {search ? (
        <form className="rk-object-picker__search" onSubmit={submitSearch}>
          <label>
            <span className="rk-sr-only">搜索{label}</span>
            <input
              type="search"
              value={search.value}
              placeholder={search.placeholder}
              onChange={(event) => search.onChange(event.target.value)}
            />
          </label>
          <button
            type="submit"
            disabled={!search.value.trim() || status === "LOADING"}
          >
            搜索当前研究
          </button>
        </form>
      ) : null}
      {status === "NOT_PUBLISHED" ? (
        <div className="rk-object-picker__notice" data-state="NOT_PUBLISHED">
          <strong>对象选择源尚未发布</strong>
          <span>
            {statusText ?? "请先到产生该对象的页面完成工作，再返回这里选择。"}
          </span>
        </div>
      ) : status === "ERROR" ? (
        <div
          className="rk-object-picker__notice"
          data-state="ERROR"
          role="alert"
        >
          <strong>对象列表读取失败</strong>
          <span>{statusText}</span>
        </div>
      ) : null}
      {status === "LOADING" ? (
        <div className="rk-object-picker__loading" role="status">
          正在读取真实对象…
        </div>
      ) : null}
      {status === "READY" && options.length === 0 ? (
        <p className="rk-object-picker__empty">{emptyText}</p>
      ) : null}
      {options.length > 0 ? (
        <ul className="rk-object-picker__options">
          {options.map((option) => (
            <li key={option.id}>
              <button
                type="button"
                aria-pressed={selected.has(option.id)}
                disabled={option.disabled}
                onClick={() => toggle(option)}
              >
                <span className="rk-object-picker__mark" aria-hidden="true">
                  {selected.has(option.id) ? "✓" : ""}
                </span>
                <span>
                  <strong>{option.label}</strong>
                  {option.description ? (
                    <small>{option.description}</small>
                  ) : null}
                </span>
                {option.meta ? <em>{option.meta}</em> : null}
              </button>
            </li>
          ))}
        </ul>
      ) : null}
      {selectedIds.length > 0 ? (
        <details className="rk-object-picker__technical">
          <summary>查看技术标识</summary>
          <ul>
            {selectedIds.map((id) => (
              <li key={id}>
                <code>{id}</code>
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </fieldset>
  );
}
