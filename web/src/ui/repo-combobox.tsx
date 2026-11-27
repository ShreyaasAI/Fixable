// The repo picker (m11 A1): a shadcn combobox. `repo-select` is the trigger (role=combobox);
// each repo is a role=option, aria-disabled when the user cannot push to it.

import { Check, ChevronsUpDown, Lock } from "lucide-react";
import { useState } from "react";
import type { Repo } from "@/api";
import { cn } from "@/lib/utils";
import {
  Command,
  CommandEmpty,
  CommandInput,
  CommandItem,
  CommandList,
  Popover,
  PopoverContent,
  PopoverTrigger,
} from "./primitives";

export function RepoCombobox({
  id,
  repos,
  value,
  onChange,
  invalid,
}: {
  id: string;
  repos: Repo[] | null;
  value: string;
  onChange: (name: string) => void;
  invalid?: Record<string, unknown>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <button
          id={id}
          type="button"
          role="combobox"
          aria-expanded={open}
          aria-controls={`${id}-list`}
          data-testid="repo-select"
          disabled={repos === null}
          className="flex h-11 w-full items-center justify-between gap-2 rounded-sm border border-line bg-raised px-3 text-left font-mono text-sm text-fg transition-colors duration-150 hover:border-muted aria-[invalid=true]:border-danger disabled:opacity-60"
          {...invalid}
        >
          <span className={cn("truncate", !value && "text-muted")}>
            {value || (repos === null ? "Loading…" : "Choose a repository")}
          </span>
          <ChevronsUpDown className="size-4 shrink-0 text-muted" aria-hidden="true" />
        </button>
      </PopoverTrigger>
      <PopoverContent className="min-w-72 p-0">
        <Command>
          <CommandInput placeholder="Search repos…" aria-label="Search repositories" />
          <CommandList id={`${id}-list`}>
            <CommandEmpty>No repo matches.</CommandEmpty>
            {repos?.map((r) => (
              <CommandItem
                key={r.full_name}
                value={r.full_name}
                disabled={!r.can_push}
                onSelect={(name) => {
                  onChange(name);
                  setOpen(false);
                }}
              >
                <Check
                  aria-hidden="true"
                  className={cn("size-4 shrink-0 text-accent", value !== r.full_name && "opacity-0")}
                />
                <span className="truncate">{r.full_name}</span>
                {!r.can_push && (
                  <span className="ml-auto inline-flex shrink-0 items-center gap-1 text-xs">
                    <Lock className="size-3" aria-hidden="true" />
                    <span>read only</span>
                  </span>
                )}
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
