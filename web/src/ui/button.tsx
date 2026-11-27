import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import type { ComponentProps } from "react";
import { cn } from "@/lib/utils";

// shadcn Button, restyled for the workbench: square-ish, 1 px borders, no shadows.
export const buttonVariants = cva(
  "inline-flex shrink-0 select-none items-center justify-center gap-2 whitespace-nowrap rounded-sm font-sans text-[15px] font-semibold transition-[background-color,color,border-color,opacity,transform] duration-150 active:translate-y-px disabled:pointer-events-none disabled:opacity-45 [&_svg]:size-[18px] [&_svg]:shrink-0",
  {
    variants: {
      kind: {
        primary: "bg-accent text-on-accent hover:bg-accent/88",
        secondary: "border border-line bg-raised text-fg hover:border-muted",
        danger: "bg-danger text-on-danger hover:bg-danger/88",
        "danger-outline": "border border-danger/70 bg-transparent text-danger hover:bg-danger/10",
        quiet: "border border-line bg-transparent text-fg hover:bg-raised",
        link: "font-mono text-[13px] font-medium text-accent underline-offset-4 hover:underline",
      },
      size: {
        md: "h-11 min-w-11 px-[18px]",
        sm: "h-9 px-3 text-sm",
        lg: "h-[52px] px-6 text-base",
      },
    },
    compoundVariants: [{ kind: "link", class: "h-auto min-w-0 px-0" }],
    defaultVariants: { kind: "secondary", size: "md" },
  },
);

export type ButtonProps = ComponentProps<"button"> &
  VariantProps<typeof buttonVariants> & { asChild?: boolean };

export function Button({ className, kind, size, asChild = false, type, ...props }: ButtonProps) {
  const Comp = asChild ? Slot : "button";
  return (
    <Comp
      className={cn(buttonVariants({ kind, size }), className)}
      {...(asChild ? {} : { type: type ?? "button" })}
      {...props}
    />
  );
}
