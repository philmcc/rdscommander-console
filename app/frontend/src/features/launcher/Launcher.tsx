import { ArrowLeft } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { apiGet } from "@/api/client";
import type { DefinitionSummary, ResourceList, ResourceRecord } from "@/api/types";
import { useEnvironment } from "@/app/EnvironmentContext";
import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Dialog, DialogContent, DialogDescription, DialogTitle } from "@/components/ui/dialog";
import { loadCatalogue } from "@/features/tasks/catalogue";
import { useTask } from "@/features/tasks/TaskContext";
import { ENVIRONMENT_FREE_KINDS } from "@/features/tasks/TaskPanel";
import { describeError } from "@/lib/describeError";
import { resourceIdFromArn } from "@/lib/resourceId";
import {
  initialOptionsFor,
  launcherKeywords,
  launcherMatchScore,
  needsResource,
  orderGroupsByBestMatch,
} from "./launchOptions";

/** The resource kinds an entry can be started on, besides the environment itself. */
const RESOURCE_KINDS = ["instance", "cluster", "parameter_group", "snapshot"];

/** The resource kinds `definition` can be started on, besides the environment itself. */
function applicableKinds(definition: DefinitionSummary): string[] {
  return definition.applies_to.filter((kind) => RESOURCE_KINDS.includes(kind));
}

/**
 * Resource kinds as plural words for a sentence.
 *
 * @example
 * kindsInWords(["instance", "parameter_group"]); // "instances or parameter groups"
 */
function kindsInWords(kinds: string[]): string {
  const plurals = kinds.map((kind) => `${kind.replaceAll("_", " ")}s`);
  const last = plurals.pop();
  return plurals.length > 0 ? `${plurals.join(", ")} or ${last}` : (last ?? "resources");
}

/** The step after choosing an entry that needs a resource: `resources` is `null` while they load or when `error` says why not. */
interface ResourceChoice {
  definition: DefinitionSummary;
  resources: ResourceRecord[] | null;
  error: string | null;
}

/**
 * The launcher, opened with ⌘K or the "Start a task" button (the `rdscommander:launcher` window event).
 * It lists every catalogue entry by title and group and filters as you type, ranking what matches: the
 * groups by their best match and the entries within a group by their own, so the entry that matches best is
 * first and selected, and Enter starts it (see `orderGroupsByBestMatch`). An entry that acts on the
 * environment opens its task for the selected environment; one that acts on resources first lists the
 * resources of every kind it applies to, as far as the backend says the task applies to them (the
 * `applicable_to` query), and the environment itself when the task applies to that too and does not need a
 * resource. Each resource has its kind beside it. A task started on a resource gets the options that
 * resource determines (see `initialOptionsFor`).
 */
export function Launcher() {
  const { selected } = useEnvironment();
  const { open } = useTask();
  const [visible, setVisible] = useState(false);
  const [definitions, setDefinitions] = useState<DefinitionSummary[] | null>(null);
  const [catalogueError, setCatalogueError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [choice, setChoice] = useState<ResourceChoice | null>(null);
  const [search, setSearch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Identifies the latest choice, so a resource list that arrives after going back or closing is dropped.
  const choiceSerial = useRef(0);

  useEffect(() => {
    const show = () => setVisible(true);
    window.addEventListener("rdscommander:launcher", show);
    return () => window.removeEventListener("rdscommander:launcher", show);
  }, []);

  useEffect(() => {
    if (!visible) return;
    let cancelled = false;
    loadCatalogue().then(
      (loaded) => {
        if (cancelled) return;
        setDefinitions(loaded);
        setCatalogueError(null);
      },
      (caught: unknown) => {
        if (!cancelled) setCatalogueError(describeError(caught));
      },
    );
    return () => {
      cancelled = true;
    };
  }, [visible]);

  const groups = useMemo(() => {
    const byGroup = new Map<string, DefinitionSummary[]>();
    for (const definition of definitions ?? []) {
      byGroup.set(definition.group, [...(byGroup.get(definition.group) ?? []), definition]);
    }
    return [...byGroup.entries()];
  }, [definitions]);
  // The command list ranks entries within a group but leaves the groups in catalogue order.
  const rankedGroups = useMemo(() => orderGroupsByBestMatch(groups, search), [groups, search]);

  const backToTasks = () => {
    choiceSerial.current += 1;
    setChoice(null);
    setSearch("");
    inputRef.current?.focus();
  };

  const close = () => {
    choiceSerial.current += 1;
    setVisible(false);
    setChoice(null);
    setNotice(null);
    setSearch("");
  };

  const startTask = (definition: DefinitionSummary, resource?: ResourceRecord) => {
    close();
    if (resource) {
      open({
        kind: definition.kind,
        environmentSlug: resource.environment_slug,
        resourceId: resourceIdFromArn(resource.arn),
        resourceIdentifier: resource.identifier,
        resourceKind: resource.kind,
        targetKey: resource.target_key,
        initialOptions: initialOptionsFor(definition, resource),
      });
    } else {
      open({
        kind: definition.kind,
        environmentSlug: ENVIRONMENT_FREE_KINDS.has(definition.kind) ? undefined : selected?.slug,
      });
    }
  };

  const choose = async (definition: DefinitionSummary) => {
    if (!selected && !ENVIRONMENT_FREE_KINDS.has(definition.kind)) {
      setNotice("There is no environment to run this task in.");
      return;
    }
    const kinds = applicableKinds(definition);
    if (kinds.length === 0 || !selected) {
      startTask(definition);
      return;
    }
    choiceSerial.current += 1;
    const serial = choiceSerial.current;
    setNotice(null);
    setSearch("");
    setChoice({ definition, resources: null, error: null });
    try {
      const lists = await Promise.all(
        kinds.map((kind) =>
          apiGet<ResourceList>(`/api/v1/environments/${selected.slug}/resources`, {
            kind,
            applicable_to: definition.kind,
          }),
        ),
      );
      if (serial === choiceSerial.current) {
        setChoice({ definition, resources: lists.flatMap((list) => list.resources), error: null });
      }
    } catch (caught) {
      if (serial === choiceSerial.current)
        setChoice({ definition, resources: null, error: describeError(caught) });
    }
  };

  const picking = choice !== null;
  // With nothing typed, an empty list means the backend found no resource the task applies to.
  const emptyMessage =
    choice && selected && search === ""
      ? `No ${kindsInWords(applicableKinds(choice.definition))} this task applies to in ${selected.display_name}.`
      : "Nothing matches.";
  const loadingResources = choice !== null && choice.resources === null && choice.error === null;
  return (
    <Dialog open={visible} onOpenChange={(next) => (next ? setVisible(true) : close())}>
      <DialogContent showCloseButton={false} className="overflow-hidden p-0 sm:max-w-lg">
        <DialogTitle className="sr-only">Start a task</DialogTitle>
        <DialogDescription className="sr-only">
          Search the task catalogue, then choose a resource when the task needs one.
        </DialogDescription>
        <Command
          label={choice ? `Choose a resource for ${choice.definition.title}` : "Start a task"}
          filter={launcherMatchScore}
        >
          {choice && (
            <div className="border-b border-hairline px-2 py-1">
              <Button variant="ghost" size="sm" onClick={backToTasks}>
                <ArrowLeft aria-hidden="true" />
                All tasks
              </Button>
            </div>
          )}
          <CommandInput
            ref={inputRef}
            value={search}
            onValueChange={setSearch}
            placeholder={choice ? `Choose a resource for ${choice.definition.title}…` : "Start a task…"}
          />
          <CommandList>
            {catalogueError && (
              <p role="alert" className="px-3 py-2 text-sm text-status-red">
                Could not load the task catalogue: {catalogueError}
              </p>
            )}
            {notice && (
              <p role="alert" className="px-3 py-2 text-sm text-status-red">
                {notice}
              </p>
            )}
            {choice?.error && (
              <p role="alert" className="px-3 py-2 text-sm text-status-red">
                Could not list resources: {choice.error}
              </p>
            )}
            {(loadingResources || (!picking && definitions === null && !catalogueError)) && (
              <p role="status" className="px-3 py-2 text-sm text-muted">
                Loading…
              </p>
            )}
            {!loadingResources && !catalogueError && !choice?.error && definitions !== null && (
              <CommandEmpty>{emptyMessage}</CommandEmpty>
            )}
            {!picking &&
              rankedGroups.map(([group, items]) => (
                <CommandGroup key={group} heading={group}>
                  {items.map((definition) => (
                    <CommandItem
                      key={definition.kind}
                      value={definition.title}
                      keywords={launcherKeywords(definition)}
                      onSelect={() => void choose(definition)}
                    >
                      <div>
                        <div>{definition.title}</div>
                        <div className="text-xs text-muted">{definition.summary}</div>
                      </div>
                    </CommandItem>
                  ))}
                </CommandGroup>
              ))}
            {choice?.resources && selected && (
              <CommandGroup heading={`${choice.definition.title} in ${selected.display_name}`}>
                {choice.definition.applies_to.includes("environment") &&
                  !needsResource(choice.definition) && (
                    <CommandItem
                      value={`${selected.slug} ${selected.display_name} environment`}
                      onSelect={() => startTask(choice.definition)}
                    >
                      <span>{selected.display_name}</span>
                      <span className="ml-2 text-xs text-muted">environment</span>
                    </CommandItem>
                  )}
                {choice.resources.map((resource) => (
                  <CommandItem
                    key={resource.arn}
                    value={`${resource.identifier} ${resource.kind} ${resource.arn}`}
                    onSelect={() => startTask(choice.definition, resource)}
                  >
                    <span className="font-mono">{resource.identifier}</span>
                    <span className="ml-2 text-xs text-muted">{resource.kind.replaceAll("_", " ")}</span>
                  </CommandItem>
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </DialogContent>
    </Dialog>
  );
}
