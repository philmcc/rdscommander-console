import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";
import type { WidgetProps } from "../SchemaForm";
import { isProtectedTagKey, TagsField } from "./TagsField";

describe("isProtectedTagKey", () => {
  it("protects the rdscommander: and aws: prefixes only", () => {
    expect(isProtectedTagKey("rdscommander:managed")).toBe(true);
    expect(isProtectedTagKey("aws:cloudformation:stack-name")).toBe(true);
    expect(isProtectedTagKey("team")).toBe(false);
    expect(isProtectedTagKey("my-aws:thing")).toBe(false);
  });
});

function renderField(overrides: Partial<WidgetProps>) {
  const onChange = vi.fn();
  render(
    <TagsField
      name="tags"
      schema={{ type: "object" }}
      ui={{ widget: "tags", label: "Tags" }}
      value={{}}
      onChange={onChange}
      context={{ environmentSlug: "sandbox", resourceId: "abc" }}
      formValue={{}}
      {...overrides}
    />,
  );
  return onChange;
}

describe("TagsField", () => {
  it("adds a tag and emits it as a map", async () => {
    const onChange = renderField({});
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    await userEvent.type(screen.getByLabelText("Tag key 1"), "team");
    await userEvent.type(screen.getByLabelText("Tag value 1"), "orders");
    expect(onChange).toHaveBeenLastCalledWith({ team: "orders" });
  });

  it("shows the tags it is given", () => {
    renderField({ value: { team: "orders", env: "prod" } });
    expect(screen.getByLabelText("Tag key 1")).toHaveValue("team");
    expect(screen.getByLabelText("Tag value 1")).toHaveValue("orders");
    expect(screen.getByLabelText("Tag key 2")).toHaveValue("env");
  });

  it("refuses a protected key inline and leaves the row out of the emitted value", async () => {
    const onChange = renderField({});
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    await userEvent.type(screen.getByLabelText("Tag key 1"), "rdscommander:x");
    expect(screen.getByText(/cannot be changed here/)).toBeInTheDocument();
    expect(screen.getByLabelText("Tag key 1")).toHaveAttribute("aria-invalid", "true");
    expect(onChange).toHaveBeenLastCalledWith({});
  });

  it("removes a row and emits what is left", async () => {
    const onChange = renderField({ value: { team: "orders", env: "prod" } });
    await userEvent.click(screen.getByRole("button", { name: "Remove tag 1" }));
    expect(onChange).toHaveBeenLastCalledWith({ env: "prod" });
  });

  it("refuses a key used twice and keeps the first", async () => {
    const onChange = renderField({ value: { team: "orders" } });
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    await userEvent.type(screen.getByLabelText("Tag key 2"), "team");
    expect(screen.getByText(/already used/)).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith({ team: "orders" });
  });

  it("with keys_only lists keys without values and emits an array", async () => {
    const onChange = renderField({
      ui: { widget: "tags", keys_only: true, label: "Tags to remove" },
      value: [],
    });
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    expect(screen.queryByLabelText("Tag value 1")).toBeNull();
    await userEvent.type(screen.getByLabelText("Tag key 1"), "old");
    expect(onChange).toHaveBeenLastCalledWith(["old"]);
  });

  it("with keys_only refuses a protected key too", async () => {
    const onChange = renderField({ ui: { widget: "tags", keys_only: true }, value: [] });
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    await userEvent.type(screen.getByLabelText("Tag key 1"), "aws:x");
    expect(screen.getByText(/cannot be changed here/)).toBeInTheDocument();
    expect(onChange).toHaveBeenLastCalledWith([]);
  });

  it("shows the rows of a new value when the form resets or restores a draft", () => {
    const onChange = vi.fn();
    const props = {
      name: "tags",
      schema: { type: "object" },
      ui: { widget: "tags" },
      onChange,
      context: {},
      formValue: {},
    };
    const { rerender } = render(<TagsField {...props} value={{ team: "orders" }} />);
    expect(screen.getByLabelText("Tag key 1")).toHaveValue("team");
    rerender(<TagsField {...props} value={{ env: "prod", owner: "sam" }} />);
    expect(screen.getByLabelText("Tag key 1")).toHaveValue("env");
    expect(screen.getByLabelText("Tag key 2")).toHaveValue("owner");
    rerender(<TagsField {...props} value={{}} />);
    expect(screen.queryByLabelText("Tag key 1")).toBeNull();
  });

  it("keeps a refused row on screen while the value it emitted comes back as the prop", async () => {
    function Harness() {
      const [tags, setTags] = useState<unknown>({ team: "orders" });
      return (
        <TagsField
          name="tags"
          schema={{ type: "object" }}
          ui={{ widget: "tags" }}
          value={tags}
          onChange={setTags}
          context={{}}
          formValue={{}}
        />
      );
    }
    render(<Harness />);
    await userEvent.click(screen.getByRole("button", { name: "Add tag" }));
    await userEvent.type(screen.getByLabelText("Tag key 2"), "aws:x");
    expect(screen.getByLabelText("Tag key 2")).toHaveValue("aws:x");
    expect(screen.getByText(/cannot be changed here/)).toBeInTheDocument();
    expect(screen.getByLabelText("Tag key 1")).toHaveValue("team");
  });
});
