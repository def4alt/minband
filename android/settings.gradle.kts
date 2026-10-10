pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
        // ClikaRT Kotlin AAR, unzipped from clika-sdk/clika-runtime-maven-0.6.4.zip (machine-local path).
        maven(url = uri("C:/Users/piotr/Desktop/Moje/D4D Seoul/clika-sdk/maven"))
    }
}

rootProject.name = "MinBand"
include(":app")
