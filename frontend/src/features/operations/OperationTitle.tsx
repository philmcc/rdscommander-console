/**
 * An operation's title, which reads `<what>: <identifier>`, with the identifier in monospace. A title
 * without that shape is shown as it is.
 *
 * @example
 * <OperationTitle title="Reboot instance: demo-orders" />
 */
export function OperationTitle({ title }: { title: string }) {
  const separator = title.indexOf(": ");
  if (separator < 0) return <>{title}</>;
  return (
    <>
      {title.slice(0, separator + 2)}
      <span className="font-mono">{title.slice(separator + 2)}</span>
    </>
  );
}
