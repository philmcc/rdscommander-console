import { EstateListPage } from "./EstateListPage";

/** The instances of the selected environment, with a button that creates one from the template. */
export default function InstancesPage() {
  return (
    <EstateListPage
      kind="instance"
      title="Instances"
      createKind="instance_create"
      createLabel="Create instance"
    />
  );
}
