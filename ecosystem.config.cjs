module.exports = {
  apps: [
    {
      name: "labbit-api",
      cwd: "/opt/labbit-py",
      script: "/opt/labbit-py/start.sh",
      interpreter: "/usr/bin/bash",
      autorestart: true,
      watch: false,
      max_restarts: 10,
      env: {
        NEOSOFT_LOGIN_USERNAME: "",
        NEOSOFT_LOGIN_PASSWORD: "",
        DELIVERY_API_BASE: "http://127.0.0.1:8000",
        REPORT_PUBLIC_BASE: "http://127.0.0.1:8000",
        WHATSAPP_ENDPOINT: "",
        WHATSAPP_API_KEY: "",
        SEND_REPORTS_TEMPLATE: "reports_pdf",
        WHATSAPP_LANGUAGE: "en",
        DEFAULT_PHONE: "",
        FALLBACK_PHONE: ""
      }
    }
  ]
};
