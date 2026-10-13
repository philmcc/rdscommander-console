import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { IdentifierConfirm } from "./IdentifierConfirm";

function setup() {
  const onConfirmed = vi.fn();
  render(<IdentifierConfirm identifier="demo-orders" onConfirmed={onConfirmed} />);
  const input = screen.getByLabelText(/Type demo-orders to confirm/);
  return { onConfirmed, input };
}

describe("IdentifierConfirm", () => {
  it("starts unconfirmed and reports true only for the exact identifier, with the text as typed", async () => {
    const { onConfirmed, input } = setup();
    expect(onConfirmed).toHaveBeenLastCalledWith(false, "");
    expect(onConfirmed).not.toHaveBeenCalledWith(true, expect.anything());
    await userEvent.type(input, "demo-order");
    expect(onConfirmed).not.toHaveBeenCalledWith(true, expect.anything());
    await userEvent.type(input, "s");
    expect(onConfirmed).toHaveBeenLastCalledWith(true, "demo-orders");
  });

  it("reports false for a case-different entry", async () => {
    const { onConfirmed, input } = setup();
    await userEvent.type(input, "Demo-Orders");
    expect(input).toHaveValue("Demo-Orders");
    expect(onConfirmed).not.toHaveBeenCalledWith(true, expect.anything());
    expect(onConfirmed).toHaveBeenLastCalledWith(false, "Demo-Orders");
  });

  it("reports false once a trailing character follows the identifier", async () => {
    const { onConfirmed, input } = setup();
    await userEvent.type(input, "demo-orders");
    expect(onConfirmed).toHaveBeenLastCalledWith(true, "demo-orders");
    await userEvent.type(input, "x");
    expect(onConfirmed).toHaveBeenLastCalledWith(false, "demo-ordersx");
  });

  it("reports false again after a character is deleted from a matching entry", async () => {
    const { onConfirmed, input } = setup();
    await userEvent.type(input, "demo-orders");
    expect(onConfirmed).toHaveBeenLastCalledWith(true, "demo-orders");
    await userEvent.type(input, "{Backspace}");
    expect(input).toHaveValue("demo-order");
    expect(onConfirmed).toHaveBeenLastCalledWith(false, "demo-order");
  });
});
