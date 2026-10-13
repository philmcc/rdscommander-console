/**
 * The 2px indeterminate strip under the top bar while any operation runs.
 *
 * Inactive it keeps its height (so the page does not shift) and is hidden from assistive technology;
 * active it is an indeterminate progress bar named "Operations running".
 */
export function ProgressStrip({ active }: { active: boolean }) {
  if (!active) return <div aria-hidden="true" className="h-0.5 w-full" />;
  return (
    <div role="progressbar" aria-label="Operations running" className="relative h-0.5 w-full overflow-hidden">
      <div className="progress-strip absolute inset-0" />
    </div>
  );
}
