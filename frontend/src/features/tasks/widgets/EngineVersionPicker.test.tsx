import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import { EngineVersionPicker } from "./EngineVersionPicker";

afterEach(() => vi.unstubAllGlobals());

function stubVersions() {
  const fetchSpy = vi.fn(async (input: RequestInfo | URL) => {
    const path = String(input);
    if (path === "/api/v1/parameters/engine-versions?environment_slug=sandbox&engine=postgres")
      return Response.json({
        versions: [
          { engine: "postgres", engine_version: "15.8", parameter_group_family: "postgres15" },
          { engine: "postgres", engine_version: "16.10", parameter_group_family: "postgres16" },
          { engine: "postgres", engine_version: "16.4", parameter_group_family: "postgres16" },
        ],
      });
    return new Response("not found", { status: 404 });
  });
  vi.stubGlobal("fetch", fetchSpy);
  return fetchSpy;
}

describe("EngineVersionPicker on a create", () => {
  it("lists every version of the hinted engine, newest first, with its family", async () => {
    stubVersions();
    const onChange = vi.fn();
    render(
      <EngineVersionPicker
        name="engine_version"
        schema={{ type: "string" }}
        ui={{ widget: "engine_version", label: "Engine version", engine: "postgres" }}
        value=""
        onChange={onChange}
        context={{ environmentSlug: "sandbox" }}
        formValue={{}}
      />,
    );
    await screen.findByRole("option", { name: "16.10 (postgres16)" });
    expect(screen.getAllByRole("option").map((option) => option.textContent)).toEqual([
      "Choose…",
      "16.10 (postgres16)",
      "16.4 (postgres16)",
      "15.8 (postgres15)",
    ]);
    await userEvent.selectOptions(screen.getByRole("combobox", { name: "Engine version" }), "16.4");
    expect(onChange).toHaveBeenLastCalledWith("16.4");
  });
});

describe("EngineVersionPicker on an upgrade of a resource", () => {
  function stubResourceVersions() {
    vi.stubGlobal(
      "fetch",
      vi.fn(async (input: RequestInfo | URL) => {
        const path = String(input);
        if (path === "/api/v1/resources/YXJuOmk")
          return Response.json({
            record: { facts: { engine: "postgres", engine_version: "16.4" } },
            actions: [],
            recent_operations: [],
          });
        if (path === "/api/v1/parameters/engine-versions?environment_slug=sandbox&engine=postgres")
          return Response.json({
            versions: [
              {
                engine: "postgres",
                engine_version: "16.4",
                parameter_group_family: "postgres16",
                valid_upgrade_targets: [
                  { engine_version: "16.6", is_major: false },
                  { engine_version: "17.2", is_major: true },
                  { engine_version: "16.8", is_major: false },
                ],
              },
            ],
          });
        return new Response("not found", { status: 404 });
      }),
    );
  }

  function renderPicker(upgradeKind?: "minor" | "major") {
    render(
      <EngineVersionPicker
        name="target_engine_version"
        schema={{ type: "string" }}
        ui={{
          widget: "engine_version",
          label: "Target version",
          ...(upgradeKind ? { upgrade_kind: upgradeKind } : {}),
        }}
        value=""
        onChange={vi.fn()}
        context={{ environmentSlug: "sandbox", resourceId: "YXJuOmk" }}
        formValue={{}}
      />,
    );
  }

  const optionTexts = () => screen.getAllByRole("option").map((option) => option.textContent);

  it("lists only the major targets of the current version for a major upgrade", async () => {
    stubResourceVersions();
    renderPicker("major");
    await screen.findByRole("option", { name: "17.2 (major)" });
    expect(optionTexts()).toEqual(["Choose…", "17.2 (major)"]);
  });

  it("lists only the minor targets of the current version, newest first, for a minor upgrade", async () => {
    stubResourceVersions();
    renderPicker("minor");
    await screen.findByRole("option", { name: "16.8 (minor)" });
    expect(optionTexts()).toEqual(["Choose…", "16.8 (minor)", "16.6 (minor)"]);
  });

  it("lists every target of the current version when the field names no kind of upgrade", async () => {
    stubResourceVersions();
    renderPicker();
    await screen.findByRole("option", { name: "17.2 (major)" });
    expect(optionTexts()).toEqual(["Choose…", "17.2 (major)", "16.8 (minor)", "16.6 (minor)"]);
  });
});
