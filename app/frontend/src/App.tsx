import { EnvironmentProvider } from "@/app/EnvironmentContext";
import { AppRoutes } from "@/app/routes";
import { TaskProvider } from "@/features/tasks/TaskContext";

/** Providers around the routed console. */
export default function App() {
  return (
    <EnvironmentProvider>
      <TaskProvider>
        <AppRoutes />
      </TaskProvider>
    </EnvironmentProvider>
  );
}
