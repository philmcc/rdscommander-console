import { Badge } from "@/components/ui/badge";

/** Visible on every screen that shows a simulated target so a rehearsal is never mistaken for production. */
export function SimulatedBadge({ simulated }: { simulated: boolean }) {
  if (!simulated) return null;
  return (
    <Badge variant="outline" className="border-dashed border-status-amber text-status-amber">
      simulated
    </Badge>
  );
}
