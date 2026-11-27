import { Monitor, Moon, Sun } from "lucide-react";
import { useTheme, type ThemePref } from "@/lib/theme";
import { cn } from "@/lib/utils";
import { RadioGroup, RadioGroupItem, Tooltip, TooltipContent, TooltipTrigger } from "./primitives";

const OPTIONS: { value: ThemePref; label: string; Icon: typeof Sun }[] = [
  { value: "system", label: "System", Icon: Monitor },
  { value: "dark", label: "Dark", Icon: Moon },
  { value: "light", label: "Light", Icon: Sun },
];

/** System / Dark / Light, saved per browser (m11 §2). */
export function ThemeToggle({ className }: { className?: string }) {
  const [pref, setPref] = useTheme();
  return (
    <RadioGroup
      aria-label="Theme"
      data-testid="theme-toggle"
      value={pref}
      onValueChange={(v) => setPref(v as ThemePref)}
      className={cn("rounded-sm border border-line p-0.5", className)}
    >
      {OPTIONS.map(({ value, label, Icon }) => (
        <Tooltip key={value}>
          <TooltipTrigger asChild>
            <RadioGroupItem
              value={value}
              aria-label={label}
              className="grid size-8 place-items-center rounded-xs text-muted transition-colors duration-150 hover:text-fg data-[state=checked]:bg-raised data-[state=checked]:text-accent"
            >
              <Icon className="size-4" aria-hidden="true" />
            </RadioGroupItem>
          </TooltipTrigger>
          <TooltipContent>{label}</TooltipContent>
        </Tooltip>
      ))}
    </RadioGroup>
  );
}
