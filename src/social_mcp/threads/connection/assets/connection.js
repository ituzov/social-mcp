const form = document.querySelector("#connection-form");
const token = document.querySelector("#token");
const visibility = document.querySelector("#visibility");
const submit = document.querySelector("#submit");
const submitLabel = document.querySelector("#submit-label");

function hideToken() {
    token.type = "password";
    visibility.textContent = visibility.dataset.show;
    visibility.setAttribute("aria-pressed", "false");
}

visibility.addEventListener("click", () => {
    const visible = token.type === "password";
    token.type = visible ? "text" : "password";
    visibility.textContent = visible ? visibility.dataset.hide : visibility.dataset.show;
    visibility.setAttribute("aria-pressed", String(visible));
});

form.addEventListener("submit", (event) => {
    if (form.getAttribute("aria-busy") === "true") {
        event.preventDefault();
        return;
    }
    hideToken();
    form.setAttribute("aria-busy", "true");
    submit.disabled = true;
    visibility.disabled = true;
    submitLabel.textContent = submitLabel.dataset.checking;
});

window.addEventListener("pageshow", () => {
    hideToken();
    form.setAttribute("aria-busy", "false");
    submit.disabled = false;
    visibility.disabled = false;
    submitLabel.textContent = submitLabel.dataset.idle;
});
