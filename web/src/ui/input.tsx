import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

// shadcn Input and Textarea, restyled.
export const fieldBox =
  "w-full rounded-sm border border-line bg-raised px-3 text-[15px] text-fg placeholder:text-muted/80 transition-colors duration-150 hover:border-muted aria-[invalid=true]:border-danger disabled:opacity-50";

export function Input({ className, ...props }: ComponentProps<"input">) {
  return <input className={cn(fieldBox, "h-11", className)} {...props} />;
}

export function Textarea({ className, ...props }: ComponentProps<"textarea">) {
  return (
    <textarea
      className={cn(fieldBox, "min-h-28 resize-y py-2.5 leading-relaxed", className)}
      {...props}
    />
  );
}
