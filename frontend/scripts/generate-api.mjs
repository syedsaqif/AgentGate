import { mkdir, readFile, writeFile } from "node:fs/promises";
import openapiTS from "openapi-typescript";

const snapshot = new URL("../../contracts/openapi.json", import.meta.url);
const outputDirectory = new URL("../src/api/generated/", import.meta.url);
const artifact = new URL("openapi.ts", outputDirectory);

const schema = JSON.parse(await readFile(snapshot, "utf8"));
const types = await openapiTS(schema, { cwd: new URL(".", snapshot) });
const generated = `${types.trimEnd()}\n\nexport const CONTRACT_VERSION = ${JSON.stringify(schema.info.version)} as const;\n`;

if (process.argv.includes("--check")) {
  const current = await readFile(artifact, "utf8").catch((error) => {
    if (error.code === "ENOENT") return null;
    throw error;
  });
  if (current?.replaceAll("\r\n", "\n") !== generated) {
    console.error("OpenAPI bindings are out of date. Run npm run api:generate and include the updated artifact.");
    process.exitCode = 1;
  } else {
    console.log("OpenAPI bindings match contracts/openapi.json.");
  }
} else {
  await mkdir(outputDirectory, { recursive: true });
  await writeFile(artifact, generated);
  console.log("Wrote src/api/generated/openapi.ts.");
}
