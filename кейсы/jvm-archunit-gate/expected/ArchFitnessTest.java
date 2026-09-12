// СГЕНЕРИРОВАНО: arch-be archunit gen (ADR-039). Не редактировать вручную —
// источник правил: CONSTRAINTS.yaml; перегенерация перезапишет файл.
// Каждое правило помечено id из CONSTRAINTS.yaml — трассировка нарушения в источник.
import com.tngtech.archunit.core.importer.ImportOption;
import com.tngtech.archunit.junit.AnalyzeClasses;
import com.tngtech.archunit.junit.ArchTest;
import com.tngtech.archunit.lang.ArchRule;

import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.classes;
import static com.tngtech.archunit.lang.syntax.ArchRuleDefinition.noClasses;

@AnalyzeClasses(packages = "com", importOptions = ImportOption.DoNotIncludeTests.class)
class ArchFitnessTest {

    @ArchTest
    static final ArchRule C_01 =
            noClasses().that().resideInAPackage("..domain..")
            .should().dependOnClassesThat().resideInAnyPackage("com.acme.infrastructure..")
            .as("[C-01] domain_not_infrastructure: ..domain.. не зависит от com.acme.infrastructure..");

    @ArchTest
    static final ArchRule C_02 =
            noClasses().that().resideInAPackage("..")
            .should().dependOnClassesThat().resideInAnyPackage("com.external.fx..")
            .as("[C-02] no_external_fx: .. не зависит от com.external.fx..");
}
