from django.db import migrations


class Migration(migrations.Migration):
    """Rename the Eligibility model to Assessment (data-preserving).

    Renames the model, its primary key (eligibility_id -> assessment_id), and the
    Answer.eligibility foreign key (-> assessment). Follow-up migration handles
    the related_name / verbose_name / index state deltas.
    """

    dependencies = [
        ("api", "0035_remove_assessmentquestionnaire_assessment_and_more"),
    ]

    operations = [
        migrations.RenameModel(old_name="Eligibility", new_name="Assessment"),
        migrations.RenameField(
            model_name="assessment",
            old_name="eligibility_id",
            new_name="assessment_id",
        ),
        migrations.RenameField(
            model_name="answer",
            old_name="eligibility",
            new_name="assessment",
        ),
    ]
