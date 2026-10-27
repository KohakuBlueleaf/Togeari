<template>
  <div />
</template>

<script setup>
import { useRoute, useRouter } from "vue-router"

import { viewPath } from "@/utils/views"

/** A run without a view: its first one. */
const route = useRoute()
useRouter().replace(viewPath(String(route.params.id)))
</script>
