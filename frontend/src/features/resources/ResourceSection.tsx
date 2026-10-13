import { type ReactNode, useId } from "react";
import { cn } from "@/lib/utils";

interface ResourceSectionProps {
  title: string;
  children: ReactNode;
  /** Anchor for a link such as `/resources/<id>#history`. */
  id?: string;
  className?: string;
}

/**
 * A titled card of a resource page; it is a named region, so a screen reader can jump between sections.
 *
 * @example
 * <ResourceSection title="Facts"><FactsGrid facts={facts} /></ResourceSection>
 */
export function ResourceSection({ title, children, id, className }: ResourceSectionProps) {
  const headingId = useId();
  return (
    <section
      id={id}
      aria-labelledby={headingId}
      className={cn("rounded-lg border border-hairline bg-surface p-4", className)}
    >
      <h2 id={headingId} className="mb-2 text-sm font-medium">
        {title}
      </h2>
      {children}
    </section>
  );
}
