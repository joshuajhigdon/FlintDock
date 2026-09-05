/* A read-only guide: no runCommand, manager relay, teleport or world mutations. */
import { ActionFormData, ModalFormData } from "@minecraft/server-ui";
import { REFERENCE } from "./reference.js";

const SIZE = 8;
const plain = value => String(value ?? "").replace(/§./g, "").replace(/[\x00-\x1f\x7f]/g, " ").trim();
const categories = ["Core server", "Admin tools", "Restart manager", "Installed mods"];
const searchable = entry => [entry.title, entry.syntax, entry.summary, entry.pack, entry.status].join(" ").toLowerCase();

export function createHelp({ showForm, tell, requireOperator }) {
    async function choose(player, title, body, buttons) {
        const form = new ActionFormData().title(title).body(body);
        for (const [key, label] of buttons) form.button(label);
        const response = await showForm(player, form);
        if (!requireOperator(player) || !response || response.canceled) return undefined;
        return buttons[response.selection]?.[0];
    }

    async function detail(player, entry) {
        const text = [entry.syntax, entry.summary,
            `Where: ${entry.where || "Pack-specific"}\nPermission: ${entry.permission || "Pack-specific"}\nCheats: ${entry.cheats || "Unknown"}`,
            `Example (reference only): ${entry.example || "Check pack documentation"}`, entry.notes,
            `Status: ${entry.status}`, entry.evidence, `Source: ${entry.source}`].filter(Boolean).join("\n\n");
        for (;;) {
            const result = await choose(player, "Command details", text, [
                ["chat", "Send reference to my chat"], ["back", "Back to results"], ["close", "Close help"],
            ]);
            if (result === "chat") {
                // Keep messages short enough for chat, without executing their contents.
                for (let i = 0; i < text.length; i += 450) tell(player, text.slice(i, i + 450));
            } else return result === "back";
        }
    }

    async function browse(player, query = "") {
        if (!requireOperator(player)) return;
        let category = "", search = plain(query).slice(0, 100).toLowerCase(), page = 0;
        let home = !search;
        tell(player, "§aCommand help§r · Core server, /admin: tools, restart manager and installed mods. Close chat to open the guide.");
        while (requireOperator(player)) {
            if (home) {
                const packInfo = REFERENCE.packs.length ? REFERENCE.packs.map(pack =>
                    `${pack.name}: ${pack.status} (${pack.count} documented/discovered entries)`).join("\n") :
                    "No additional installed mod commands were found at the last rebuild.";
                const result = await choose(player, "Server command help", REFERENCE.notes +
                    "\n\n" + packInfo + "\n\nRebuild in Launcher → Mods after changing packs.\n" +
                    REFERENCE.warnings.join("\n"), [
                    ...categories.map(name => [name, `${name}\n§7${REFERENCE.entries.filter(e => e.category === name).length} reference entries`]),
                    ["search", "Search all commands"], ["close", "Close help"],
                ]);
                if (result === undefined || result === "close") return;
                category = result === "search" ? "" : result; search = ""; page = 0; home = false;
                if (result !== "search") continue;
            } else {
                const matches = REFERENCE.entries.filter(entry => (!category || entry.category === category) &&
                    (!search || searchable(entry).includes(search)));
                const pages = Math.max(1, Math.ceil(matches.length / SIZE));
                page = Math.min(page, pages - 1);
                const visible = matches.slice(page * SIZE, (page + 1) * SIZE);
                const result = await choose(player, category || "Search commands",
                    `${matches.length} result(s) · Page ${page + 1}/${pages}` + (search ? ` · “${search}”` : "") +
                    (!matches.length ? "\nNo matching entries. Dynamic mod commands need documentation from the author." : "") +
                    (category === "Installed mods" ? "\nPack availability is a snapshot from the last launcher rebuild." : ""), [
                    ...visible.map((entry, i) => [i, `${entry.title}\n§7${entry.pack ? entry.pack + " · " + entry.status : entry.summary}`]),
                    ...(page > 0 ? [["prev", "Previous page"]] : []),
                    ...(page + 1 < pages ? [["next", "Next page"]] : []),
                    ["search", "Search / change filter"], ["home", "Help home"], ["close", "Close help"],
                ]);
                if (result === undefined || result === "close") return;
                if (typeof result === "number") { if (!await detail(player, visible[result])) return; continue; }
                if (result === "home") { home = true; continue; }
                if (result === "prev") { page--; continue; }
                if (result === "next") { page++; continue; }
            }
            const response = await showForm(player, new ModalFormData().title("Search command help")
                .textField("Name, description, mod or availability", "e.g. teleport, weather, disabled", { defaultValue: search }));
            if (!requireOperator(player)) return;
            if (response && !response.canceled) { search = plain(response.formValues?.[0]).slice(0, 100).toLowerCase(); page = 0; }
        }
    }
    return { browse };
}
