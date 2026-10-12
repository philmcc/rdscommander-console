import { Badge } from "@/components/ui/badge";

const ADOPTED = "border-status-green/40 text-status-green";
const FOREIGN = "border-status-amber/40 text-status-amber";
const UNMANAGED = "border-hairline text-muted";

const LABELS: Record<string, { text: string; className: string }> = {
  unmanaged: { text: "unmanaged", className: UNMANAGED },
  adopted: { text: "adopted", className: ADOPTED },
  foreign_terraform: { text: "Terraform-owned", className: FOREIGN },
  foreign_cloudformation: { text: "CloudFormation-owned", className: FOREIGN },
};

/** Ownership classification from inventory: green adopted, amber owned by other tooling, muted unmanaged. */
export function OwnershipBadge({ ownership }: { ownership: string }) {
  const label = LABELS[ownership] ?? { text: ownership, className: UNMANAGED };
  return (
    <Badge variant="outline" className={label.className}>
      {label.text}
    </Badge>
  );
}
