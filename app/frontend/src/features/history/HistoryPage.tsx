import { useLocation, useNavigate } from "react-router";
import { useEnvironment } from "@/app/EnvironmentContext";
import { PageHeader } from "@/components/PageHeader";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ChangesView } from "./ChangesView";
import { ExecutionRecordsView } from "./ExecutionRecordsView";

const RECORDS_TAB = "records";
const CHANGES_TAB = "changes";

/**
 * What the console ran and what it saw change in the selected environment: the execution records, searchable
 * and exportable, and the change events. The tab is kept in the URL hash (`#records`, `#changes`), so a
 * view can be shared and survives a reload; without a hash the records show.
 *
 * @example
 * <Route path="history" element={<HistoryPage />} />
 */
export default function HistoryPage() {
  const { selected } = useEnvironment();
  const { hash } = useLocation();
  const navigate = useNavigate();
  const tab = hash === `#${CHANGES_TAB}` ? CHANGES_TAB : RECORDS_TAB;
  return (
    <>
      <PageHeader
        breadcrumb={
          selected ? [{ label: selected.display_name }, { label: "Activity" }] : [{ label: "Activity" }]
        }
        title="History"
      />
      {selected ? (
        <Tabs value={tab} onValueChange={(next) => navigate({ hash: `#${next}` }, { replace: true })}>
          <TabsList aria-label="History views">
            <TabsTrigger value={RECORDS_TAB}>Execution records</TabsTrigger>
            <TabsTrigger value={CHANGES_TAB}>Changes</TabsTrigger>
          </TabsList>
          <TabsContent value={RECORDS_TAB}>
            <ExecutionRecordsView environmentSlug={selected.slug} />
          </TabsContent>
          <TabsContent value={CHANGES_TAB}>
            <ChangesView environmentSlug={selected.slug} />
          </TabsContent>
        </Tabs>
      ) : (
        <p className="text-muted">Add an environment to begin.</p>
      )}
    </>
  );
}
