import { describe, expect, it } from "vitest";
import type { ParameterMetadata } from "@/api/types";
import { validateParameterValue } from "./validation";

function metadata(overrides: Partial<ParameterMetadata> = {}): ParameterMetadata {
  return {
    name: "work_mem",
    default_value: "4096",
    description: "",
    data_type: "integer",
    allowed_values: "64-2147483647",
    is_modifiable: true,
    apply_type: "dynamic",
    apply_method_default: "immediate",
    minimum_engine_version: null,
    ...overrides,
  };
}

const forceSsl = { name: "rds.force_ssl", data_type: "boolean", allowed_values: "0,1" };
const logStatement = { name: "log_statement", data_type: "string", allowed_values: "none,ddl,mod,all" };
const sharedBuffers = { name: "shared_buffers", allowed_values: "16-1073741823" };
const randomPageCost = { name: "random_page_cost", data_type: "float", allowed_values: "0-1.79769e+308" };
const sharedPreloadLibraries = {
  name: "shared_preload_libraries",
  data_type: "list",
  allowed_values: "pgaudit,pg_cron,pg_stat_statements",
};

describe("validateParameterValue", () => {
  // The same cases as the backend's test_validate_parameter_value, so the two stay in step.
  it.each<[string, Partial<ParameterMetadata>, string, string | null]>([
    ["an integer inside the range", {}, "65536", null],
    ["text for an integer", {}, "abc", "work_mem must be an integer"],
    ["a fraction for an integer", {}, "4.5", "work_mem must be an integer"],
    ["an integer below the range", {}, "10", "work_mem must be between 64 and 2147483647"],
    ["a negative integer below the range", {}, "-5", "work_mem must be between 64 and 2147483647"],
    ["a boolean choice", forceSsl, "1", null],
    ["a word for a boolean", forceSsl, "yes", "rds.force_ssl must be one of 0, 1"],
    [
      "a boolean without allowed values",
      { ...forceSsl, allowed_values: null },
      "2",
      "rds.force_ssl must be one of 0, 1",
    ],
    ["a formula for an integer", sharedBuffers, "{DBInstanceClassMemory/32768}", null],
    ["a formula inside LEAST", sharedBuffers, "LEAST({DBInstanceClassMemory/9531392},5000)", null],
    ["an enumeration choice", logStatement, "mod", null],
    [
      "a value outside the enumeration",
      logStatement,
      "everything",
      "log_statement must be one of none, ddl, mod, all",
    ],
    ["a float inside the range", randomPageCost, "1.1", null],
    ["text for a float", randomPageCost, "cheap", "random_page_cost must be a number"],
    ["nan for a float", randomPageCost, "nan", "random_page_cost must be a number"],
    [
      "a float below the range, in AWS's words",
      randomPageCost,
      "-1",
      "random_page_cost must be between 0 and 1.79769e+308",
    ],
    ["the lower bound of a range that starts below zero", { allowed_values: "-1-2147483647" }, "-1", null],
    [
      "below a range that starts below zero",
      { name: "log_min_duration_statement", allowed_values: "-1-2147483647" },
      "-2",
      "log_min_duration_statement must be between -1 and 2147483647",
    ],
    ["list elements that are all allowed", sharedPreloadLibraries, "pg_stat_statements,pgaudit", null],
    [
      "a list element that is not allowed",
      sharedPreloadLibraries,
      "pgaudit,timescaledb",
      "shared_preload_libraries must be a comma-separated list of pgaudit, pg_cron, pg_stat_statements (timescaledb is not allowed)",
    ],
    [
      "several list elements that are not allowed",
      sharedPreloadLibraries,
      "pgaudit, a, b",
      "shared_preload_libraries must be a comma-separated list of pgaudit, pg_cron, pg_stat_statements (a, b are not allowed)",
    ],
    [
      "a parameter RDS does not let anyone modify",
      { name: "rds.logical_replication", is_modifiable: false },
      "1",
      "rds.logical_replication cannot be modified",
    ],
    [
      "a formula for a parameter that cannot be modified",
      { name: "rds.logical_replication", is_modifiable: false },
      "{DBInstanceClassMemory/1}",
      "rds.logical_replication cannot be modified",
    ],
    [
      "a string parameter with no allowed values",
      { data_type: "string", allowed_values: null },
      "anything",
      null,
    ],
  ])("%s", (_description, overrides, value, expected) => {
    expect(validateParameterValue(metadata(overrides), value)).toBe(expected);
  });
});
