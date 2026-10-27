import pluginVue from "eslint-plugin-vue"
import vuePrettier from "@vue/eslint-config-prettier"

export default [
  ...pluginVue.configs["flat/recommended"],
  vuePrettier,
  {
    rules: {
      // Single-word component names (views and blocks are named e.g. Block)
      "vue/multi-word-component-names": "off",
      "vue/require-explicit-emits": "off",
      "vue/require-prop-types": "off",
      "vue/no-v-html": "warn",
    },
  },
]
