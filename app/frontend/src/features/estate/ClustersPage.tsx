import { EstateListPage } from "./EstateListPage";

/** The clusters of the selected environment, with a button that creates one from the template. */
export default function ClustersPage() {
  return (
    <EstateListPage
      kind="cluster"
      title="Clusters"
      createKind="cluster_create"
      createLabel="Create cluster"
    />
  );
}
