// Contato comercial da Amazonika Engenharia (dados do CNPJ). Para trocar, altere só aqui.
const CONTACT = {
  email: "amazonika@amazonikaengenharia.com.br",
  whatsapp: "5596981358630", // DDI + DDD + número, só dígitos
  whatsappLabel: "(96) 98135-8630",
};

(function () {
  const emailLink = document.getElementById("contact-email-link");
  const waLink = document.getElementById("contact-whatsapp-link");
  emailLink.href = "mailto:" + CONTACT.email + "?subject=" + encodeURIComponent("OrbitBase - cotação");
  emailLink.textContent = CONTACT.email;
  waLink.href = "https://wa.me/" + CONTACT.whatsapp;
  waLink.textContent = CONTACT.whatsappLabel;

  // ?plano=Pro (vindo dos botões dos planos) já escolhe o interesse
  const plano = new URLSearchParams(window.location.search).get("plano");
  const select = document.getElementById("c-interest");
  if (plano) {
    const opt = Array.from(select.options).find((o) => o.text.toLowerCase().includes(plano.toLowerCase()));
    if (opt) select.value = opt.value;
  }

  const form = document.getElementById("contact-form");
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    const channel = event.submitter ? event.submitter.dataset.channel : "email";
    const v = (id) => document.getElementById(id).value.trim();
    const lines = [
      "Olá! Gostaria de " + v("c-interest").toLowerCase() + ".",
      "",
      "Nome: " + v("c-name"),
      v("c-company") ? "Empresa: " + v("c-company") : null,
      "E-mail: " + v("c-email"),
      v("c-phone") ? "Telefone: " + v("c-phone") : null,
      v("c-message") ? "\n" + v("c-message") : null,
    ].filter((l) => l !== null);
    const text = lines.join("\n");

    if (channel === "whatsapp") {
      window.open("https://wa.me/" + CONTACT.whatsapp + "?text=" + encodeURIComponent(text), "_blank", "noopener");
    } else {
      window.location.href =
        "mailto:" + CONTACT.email +
        "?subject=" + encodeURIComponent("OrbitBase - " + v("c-interest")) +
        "&body=" + encodeURIComponent(text);
    }
  });
})();
